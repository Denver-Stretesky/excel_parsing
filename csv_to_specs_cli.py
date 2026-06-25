#!/usr/bin/env python3
"""
csv_to_specs_cli.py
===================

Same idea as ``csv_to_specs.py`` but routes each row through the Claude Code
CLI (``claude -p``) instead of the Anthropic API. Useful for prompt-tweaking
under a Claude.ai Pro/Max subscription without needing an API key.

Requirements
------------
* Claude Code CLI installed and logged in:

      claude /login

* The ``claude`` binary on your ``PATH``.

Usage
-----
    uv run csv_to_specs_cli.py sample_products.csv --output-dir specs_out
    uv run csv_to_specs_cli.py sample_products.csv --dry-run
    uv run csv_to_specs_cli.py sample_products.csv --limit 1   # one row, fast loop

Notes
-----
* Sequential by default — Claude.ai subscriptions have message caps, and
  spawning many CLI processes in parallel burns through them quickly.
* Reuses ``read_csv_rows``, ``load_schema``, ``build_user_message``,
  ``extract_json`` and ``SYSTEM_INSTRUCTIONS`` from ``csv_to_specs`` so
  prompt edits in ``BatchCreateSpec.txt`` apply to both scripts.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from csv_to_specs import (
    SYSTEM_INSTRUCTIONS,
    build_user_message,
    extract_json,
    load_schema,
    read_csv_rows,
)

DEFAULT_TIMEOUT = 300  # seconds per row


def call_claude_cli(
    *,
    user_message: str,
    system: str,
    model: str | None,
    timeout: int,
) -> str:
    """Pipe the prompt to ``claude -p`` over stdin and return the reply."""
    cmd: list[str] = ["claude", "-p", "--append-system-prompt", system]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(
        cmd,
        input=user_message,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "(no stderr)"
        raise RuntimeError(f"claude CLI exited {result.returncode}: {stderr}")
    return result.stdout.strip()


def process_row(
    *,
    row_index: int,
    headers: list[str],
    row: dict[str, str],
    prompt_text: str,
    schema_text: str | None,
    model: str | None,
    timeout: int,
    output_dir: Path,
    dry_run: bool,
) -> tuple[int, str, Path | None]:
    user_msg = build_user_message(
        prompt_text=prompt_text,
        schema_text=schema_text,
        headers=headers,
        row=row,
        row_index=row_index,
    )
    file_stem = f"row_{row_index}"

    if dry_run:
        out_path = output_dir / f"{file_stem}.prompt.txt"
        out_path.write_text(user_msg, encoding="utf-8")
        return row_index, "dry-run", out_path

    try:
        text = call_claude_cli(
            user_message=user_msg,
            system=SYSTEM_INSTRUCTIONS,
            model=model,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, RuntimeError) as exc:
        err_path = output_dir / f"{file_stem}.error.json"
        err_path.write_text(
            json.dumps({"error": str(exc), "row_index": row_index}, indent=2),
            encoding="utf-8",
        )
        return row_index, f"cli-error: {exc}", err_path

    try:
        parsed = extract_json(text)
    except json.JSONDecodeError as exc:
        err_path = output_dir / f"{file_stem}.error.json"
        err_path.write_text(
            json.dumps(
                {
                    "error": f"non-JSON response: {exc}",
                    "row_index": row_index,
                    "raw_response": text,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return row_index, "non-json-response", err_path

    out_path = output_dir / f"{file_stem}.json"
    out_path.write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return row_index, "ok", out_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert each CSV row to a spec JSON via the Claude Code CLI.",
    )
    p.add_argument("csv", type=Path, help="Path to the input CSV file.")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("specs_out"),
        help="Directory to write per-row JSONs (default: ./specs_out)",
    )
    p.add_argument(
        "--prompt-file",
        type=Path,
        default=Path(__file__).resolve().parent / "BatchCreateSpec.txt",
        help="Prompt file with extraction rules (default: BatchCreateSpec.txt next to script)",
    )
    schema_grp = p.add_mutually_exclusive_group()
    schema_grp.add_argument(
        "--schema-file",
        type=Path,
        help="Local JSON Schema file describing the target spec shape.",
    )
    schema_grp.add_argument(
        "--schema-url",
        type=str,
        help="URL to fetch the JSON Schema from.",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Pass through to `claude --model` (default: whatever the CLI uses).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-row CLI timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N rows (useful when testing the prompt).",
    )
    p.add_argument(
        "--start-row",
        type=int,
        default=1,
        help="1-based row index to start at (skip earlier rows).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't call the CLI; write the would-be prompt for each row to disk.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not args.csv.is_file():
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 2
    if not args.prompt_file.is_file():
        print(f"error: prompt file not found: {args.prompt_file}", file=sys.stderr)
        return 2
    if not args.dry_run and shutil.which("claude") is None:
        print(
            "error: `claude` CLI not found on PATH. Install Claude Code and run "
            "`claude /login`, or pass --dry-run.",
            file=sys.stderr,
        )
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompt_text = args.prompt_file.read_text(encoding="utf-8")
    schema_text = load_schema(args.schema_file, args.schema_url)

    headers, rows = read_csv_rows(args.csv)
    if not rows:
        print("error: CSV has no data rows", file=sys.stderr)
        return 2

    indexed = [pair for pair in enumerate(rows, start=1) if pair[0] >= args.start_row]
    if args.limit is not None:
        indexed = indexed[: args.limit]

    print(
        f"Processing {len(indexed)} row(s) (of {len(rows)} total) into {args.output_dir}"
        + (" [dry-run]" if args.dry_run else " via claude CLI")
    )

    successes = 0
    failures = 0
    for idx, row in indexed:
        result = process_row(
            row_index=idx,
            headers=headers,
            row=row,
            prompt_text=prompt_text,
            schema_text=schema_text,
            model=args.model,
            timeout=args.timeout,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
        _, status, path = result
        marker = "OK " if status in ("ok", "dry-run") else "ERR"
        print(f"  {marker} row {idx}: {status} -> {path}")
        if status in ("ok", "dry-run"):
            successes += 1
        else:
            failures += 1

    print(f"Done. {successes} succeeded, {failures} failed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
