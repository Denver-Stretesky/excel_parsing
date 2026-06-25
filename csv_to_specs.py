#!/usr/bin/env python3
"""
csv_to_specs.py
===============

Take a CSV of product data and turn each row into a JSON product-spec file
that conforms to the Cerve "POST /specs" schema, by sending the row plus the
BatchCreateSpec prompt to Claude.

The CSV is expected to be messy:
  * Not every spec field has a matching column.
  * One column may carry several fields (e.g. "Dimensions (HxWxL mm)" =>
    "150x60x60").
  * Header rows often contain unit hints, e.g. "Net Weight (g)",
    "Length (mm)", "Volume (ml)". The script forwards these header strings
    verbatim so Claude can pick up the units.

Usage
-----
    export ANTHROPIC_API_KEY=sk-ant-...

    uv run csv_to_specs.py products.csv \\
        --output-dir out/ \\
        --schema-file cerve_spec_schema.json    # OR --schema-url https://...

    # Dry run (no API calls) — writes the prompt that *would* be sent for
    # every row to <output_dir>/row_NNNN.prompt.txt:
    uv run csv_to_specs.py products.csv --dry-run

Notes
-----
* The schema is OPTIONAL. If supplied (file or URL) it is embedded in the
  prompt so Claude can match field names exactly. Without one, Claude is
  told to follow the field structure described in BatchCreateSpec.txt.
* Output files are named by row index: row_1.json, row_2.json...
* Failures (network, non-JSON response, etc.) are logged and the row is
  written as row_NNNN.error.json with the offending response body for
  inspection. The rest of the rows continue.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import anthropic

DEFAULT_MODEL = "claude-sonnet-4-6"  # change with --model if needed
DEFAULT_MAX_TOKENS = 8192
DEFAULT_PARALLELISM = 4
DEFAULT_RETRIES = 3


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return (headers, rows). Rows are dicts keyed by the raw header text
    (preserving any unit hints like 'Net Weight (g)')."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise SystemExit(f"CSV {path} has no header row")
        headers = [h.strip() for h in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for raw in reader:
            cleaned = {
                (k.strip() if k else ""): ("" if v is None else str(v).strip())
                for k, v in raw.items()
            }
            rows.append(cleaned)
        return headers, rows


def row_as_document(headers: list[str], row: dict[str, str]) -> str:
    """Render a single CSV row as a small "document" the model can read.

    Header text is kept verbatim so unit hints (kg, mm, ml, etc.) survive.
    Empty cells are skipped to keep the prompt compact, but we tell the
    model that absent fields should be null in the output.
    """
    lines: list[str] = []
    for header in headers:
        value = row.get(header, "")
        if value == "" or value is None:
            continue
        lines.append(f"- {header}: {value}")
    if not lines:
        return "(this row has no populated cells)"
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

def load_schema(schema_file: Path | None, schema_url: str | None) -> str | None:
    if schema_file:
        text = schema_file.read_text(encoding="utf-8")
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            return text
    if schema_url:
        with urllib.request.urlopen(schema_url, timeout=30) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, indent=2)
        except json.JSONDecodeError:
            return text
    return None


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = (
    "You are an extraction pipeline that converts a single CSV row of "
    "product data into a JSON object that conforms to the Cerve product "
    "spec schema. Reply with raw JSON ONLY — no markdown fences, no prose, "
    "no leading/trailing commentary. If a field cannot be determined from "
    "the row, use JSON null (not the string \"null\")."
)


def build_user_message(
    prompt_text: str,
    schema_text: str | None,
    headers: list[str],
    row: dict[str, str],
    row_index: int,
) -> str:
    parts: list[str] = []
    parts.append(prompt_text.rstrip())
    parts.append("")
    parts.append("# CSV context")
    parts.append(
        "The source is a single row of a CSV. The header strings are kept "
        "verbatim — pay attention to units in parentheses (e.g. 'Net Weight "
        "(g)' tells you the value's unit is grams). One column may pack "
        "several fields together; split them as needed. Columns not present "
        "in the row mean that field is unknown — emit null for those."
    )
    parts.append("")
    parts.append("# CSV headers (in original order)")
    parts.append(json.dumps(headers, ensure_ascii=False))
    parts.append("")
    parts.append(f"# Row {row_index} (only populated cells shown)")
    parts.append(row_as_document(headers, row))
    parts.append("")
    if schema_text:
        parts.append("# Target JSON Schema")
        parts.append(
            "Your output MUST validate against this schema. Use the exact "
            "field names and nesting it specifies."
        )
        parts.append("```json")
        parts.append(schema_text)
        parts.append("```")
        parts.append("")
    parts.append(
        "Return ONLY the JSON object for this row. No markdown, no commentary."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Anthropic API
# ---------------------------------------------------------------------------

def call_claude(
    *,
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    system: str,
    user_message: str,
) -> str:
    """Call the Anthropic Messages API and return the assistant text.

    The official SDK already handles retries and exponential backoff via
    its `max_retries` constructor argument, so we don't reimplement that.
    """
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    chunks: list[str] = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks).strip()


# ---------------------------------------------------------------------------
# JSON extraction (defensive — Claude is told to return raw JSON, but we
# strip accidental ```json fences just in case).
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> Any:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = _FENCE_RE.sub("", candidate).strip()
    return json.loads(candidate)


# ---------------------------------------------------------------------------
# Per-row processing
# ---------------------------------------------------------------------------

def process_row(
    *,
    row_index: int,
    headers: list[str],
    row: dict[str, str],
    prompt_text: str,
    schema_text: str | None,
    client: anthropic.Anthropic | None,
    model: str,
    max_tokens: int,
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

    assert client is not None  # guarded by main()
    try:
        text = call_claude(
            client=client,
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_INSTRUCTIONS,
            user_message=user_msg,
        )
    except Exception as exc:  # noqa: BLE001
        err_path = output_dir / f"{file_stem}.error.json"
        err_path.write_text(
            json.dumps({"error": str(exc), "row_index": row_index}, indent=2),
            encoding="utf-8",
        )
        return row_index, f"api-error: {exc}", err_path

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert each CSV row to a Cerve product-spec JSON via Claude.",
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
        help="URL to fetch the JSON Schema from (e.g. the Cerve docs).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    p.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Max output tokens per row (default: {DEFAULT_MAX_TOKENS})",
    )
    p.add_argument(
        "--parallelism",
        type=int,
        default=DEFAULT_PARALLELISM,
        help=f"Concurrent API calls (default: {DEFAULT_PARALLELISM})",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"SDK retry attempts on transient API failures (default: {DEFAULT_RETRIES})",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N rows (useful for testing).",
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
        help="Don't call the API; write the would-be prompt for each row to disk.",
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

    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompt_text = args.prompt_file.read_text(encoding="utf-8")
    schema_text = load_schema(args.schema_file, args.schema_url)

    headers, rows = read_csv_rows(args.csv)
    if not rows:
        print("error: CSV has no data rows", file=sys.stderr)
        return 2

    client: anthropic.Anthropic | None = None
    if not args.dry_run:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "error: ANTHROPIC_API_KEY is not set. Export it or pass --dry-run.",
                file=sys.stderr,
            )
            return 2
        client = anthropic.Anthropic(max_retries=args.retries)

    # Build the slice we'll process.
    indexed: list[tuple[int, dict[str, str]]] = list(enumerate(rows, start=1))
    indexed = [pair for pair in indexed if pair[0] >= args.start_row]
    if args.limit is not None:
        indexed = indexed[: args.limit]

    print(
        f"Processing {len(indexed)} row(s) "
        f"(of {len(rows)} total) into {args.output_dir}"
        + (" [dry-run]" if args.dry_run else f" with {args.model}")
    )

    successes = 0
    failures = 0

    def _do(pair: tuple[int, dict[str, str]]) -> tuple[int, str, Path | None]:
        idx, row = pair
        return process_row(
            row_index=idx,
            headers=headers,
            row=row,
            prompt_text=prompt_text,
            schema_text=schema_text,
            client=client,
            model=args.model,
            max_tokens=args.max_tokens,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )

    if args.parallelism <= 1 or args.dry_run:
        results = [_do(pair) for pair in indexed]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=args.parallelism) as pool:
            futures = [pool.submit(_do, pair) for pair in indexed]
            for fut in as_completed(futures):
                results.append(fut.result())

    results.sort(key=lambda r: r[0])
    for idx, status, path in results:
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
