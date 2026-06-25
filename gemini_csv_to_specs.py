#!/usr/bin/env python3
"""
gemini_csv_to_specs.py
======================

Same idea as ``csv_to_specs.py`` but uses Google Gemini (``gemini-3.5-flash``)
instead of Anthropic. The full JSON schema is included in the prompt text;
the model is told to return raw JSON via ``response_mime_type=application/json``.

Importable: ``supplier_pipeline.py`` calls ``process_csv(...)`` directly so the
whole thing can be frozen with PyInstaller (no subprocess, no ``uv run``).

Standalone CLI usage:
    export GEMINI_API_KEY=...
    uv run gemini_csv_to_specs.py products.csv \\
        --prompt-file ExtractSpec.txt \\
        --schema-file spec_schema.json \\
        --output-dir specs_out/
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_PARALLELISM = 4
DEFAULT_RETRIES = 4
DEFAULT_TEMPERATURE = 0.1

SYSTEM_INSTRUCTIONS = (
    "You are an extraction pipeline that converts a single CSV row of "
    "product data into a JSON object that conforms to the Cerve product "
    "spec schema. Reply with raw JSON ONLY — no markdown fences, no prose, "
    "no leading/trailing commentary. If a field cannot be determined from "
    'the row, use JSON null (not the string "null").'
)


# ---------------------------------------------------------------------------
# CSV reading (duplicated from csv_to_specs.py so this file has no anthropic dep)
# ---------------------------------------------------------------------------
def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
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
            return json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            return text
    if schema_url:
        with urllib.request.urlopen(schema_url, timeout=30) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
        try:
            return json.dumps(json.loads(text), indent=2)
        except json.JSONDecodeError:
            return text
    return None


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
def build_user_message(
    prompt_text: str,
    schema_text: str | None,
    headers: list[str],
    row: dict[str, str],
    row_index: int,
) -> str:
    parts: list[str] = [prompt_text.rstrip(), ""]
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
# Gemini call
# ---------------------------------------------------------------------------
def _get_genai_client():
    """Lazy import + construct a genai client so PyInstaller doesn't choke at module load."""
    from google import genai

    return genai.Client()


_RETRY_DELAY_RE = re.compile(r"retry(?:Delay)?['\"]?[:\s]+['\"]?(\d+(?:\.\d+)?)s")


def _retry_delay_for(e: Exception) -> float | None:
    """If the error looks like a 429 with a server-suggested delay, return it."""
    msg = str(e)
    if "429" not in msg and "RESOURCE_EXHAUSTED" not in msg:
        return None
    m = _RETRY_DELAY_RE.search(msg)
    return float(m.group(1)) if m else 60.0  # default to 60s for free-tier 5-RPM window


def call_gemini(
    *,
    client,
    model: str,
    system: str,
    user_message: str,
    retries: int,
    temperature: float,
) -> str:
    from google.genai import types

    last_err: str | None = None
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=temperature,
                ),
            )
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
            raise RuntimeError("empty response from Gemini")
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            if attempt == retries - 1:
                raise RuntimeError(last_err) from e
            rate_limit_delay = _retry_delay_for(e)
            if rate_limit_delay is not None:
                print(f"  [RATE LIMIT] sleeping {rate_limit_delay:.0f}s before retry")
                time.sleep(rate_limit_delay + 2)
            else:
                time.sleep(min(2**attempt, 10))
    raise RuntimeError(last_err or "unknown gemini error")


# ---------------------------------------------------------------------------
# JSON extraction
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
def _fmt(v) -> str:
    """Compact repr for log lines — short strings unquoted, fall back to repr."""
    if v is None or v == "":
        return "—"
    s = str(v)
    return s if len(s) < 60 else s[:57] + "…"


def process_row(
    *,
    row_index: int,
    total: int,
    headers: list[str],
    row: dict[str, str],
    prompt_text: str,
    schema_text: str | None,
    client,
    model: str,
    output_dir: Path,
    retries: int,
    temperature: float,
    dry_run: bool,
) -> tuple[int, str, Path | None]:
    user_msg = build_user_message(prompt_text, schema_text, headers, row, row_index)
    stem = f"row_{row_index}"
    tag = f"[row {row_index}/{total}]"
    start = time.time()

    if dry_run:
        out_path = output_dir / f"{stem}.prompt.txt"
        out_path.write_text(user_msg, encoding="utf-8")
        print(f"  {tag} dry-run → {out_path.name}")
        return row_index, "dry-run", out_path

    try:
        text = call_gemini(
            client=client,
            model=model,
            system=SYSTEM_INSTRUCTIONS,
            user_message=user_msg,
            retries=retries,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001
        err_path = output_dir / f"{stem}.error.json"
        err_path.write_text(
            json.dumps({"error": str(exc), "row_index": row_index}, indent=2),
            encoding="utf-8",
        )
        print(f"  {tag} ✗ {time.time() - start:.1f}s  api-error: {exc}")
        return row_index, f"api-error: {exc}", err_path

    try:
        parsed = extract_json(text)
    except json.JSONDecodeError as exc:
        err_path = output_dir / f"{stem}.error.json"
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
        print(f"  {tag} ✗ {time.time() - start:.1f}s  non-JSON response from model")
        return row_index, "non-json-response", err_path

    out_path = output_dir / f"{stem}.json"
    out_path.write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    elapsed = time.time() - start
    name = _fmt(parsed.get("name"))
    brand = _fmt(parsed.get("brand"))
    uom = _fmt(parsed.get("unit_of_measure"))
    # Pull a GTIN-ish reference if extracted, for confidence in matching later
    refs = parsed.get("references") or {}
    ean = refs.get("EAN_13") or refs.get("UPC_A") or refs.get("EAN_8") or "—"
    print(
        f"  {tag} ✓ {elapsed:.1f}s  name={name!r}  brand={brand!r}  uom={uom!r}  ean={ean}"
    )
    return row_index, "ok", out_path


# ---------------------------------------------------------------------------
# Public entry point (called by supplier_pipeline.py)
# ---------------------------------------------------------------------------
def process_csv(
    *,
    csv_path: Path,
    prompt_file: Path,
    schema_file: Path | None,
    output_dir: Path,
    model: str = DEFAULT_MODEL,
    parallelism: int = DEFAULT_PARALLELISM,
    retries: int = DEFAULT_RETRIES,
    temperature: float = DEFAULT_TEMPERATURE,
    limit: int | None = None,
    start_row: int = 1,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Generate spec JSONs for every row. Returns (successes, failures)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_text = prompt_file.read_text(encoding="utf-8")
    schema_text = load_schema(schema_file, None) if schema_file else None
    headers, rows = read_csv_rows(csv_path)
    if not rows:
        raise SystemExit("CSV has no data rows")

    client = None if dry_run else _get_genai_client()

    indexed = [(i, r) for i, r in enumerate(rows, start=1) if i >= start_row]
    if limit is not None:
        indexed = indexed[:limit]
    print(
        f"Processing {len(indexed)} row(s) (of {len(rows)} total) into {output_dir}"
        + (" [dry-run]" if dry_run else f" with {model}")
    )

    total = len(indexed)

    def _do(pair):
        idx, row = pair
        return process_row(
            row_index=idx,
            total=total,
            headers=headers,
            row=row,
            prompt_text=prompt_text,
            schema_text=schema_text,
            client=client,
            model=model,
            output_dir=output_dir,
            retries=retries,
            temperature=temperature,
            dry_run=dry_run,
        )

    successes = failures = 0
    if parallelism <= 1 or dry_run:
        results = [_do(p) for p in indexed]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            for fut in as_completed([pool.submit(_do, p) for p in indexed]):
                results.append(fut.result())
    # process_row already printed per-row lines as they ran; here we just tally.
    for idx, status, path in results:
        if status in ("ok", "dry-run"):
            successes += 1
        else:
            failures += 1
    print(f"Done. {successes} succeeded, {failures} failed.")
    return successes, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert each CSV row to a spec JSON via Gemini."
    )
    ap.add_argument("csv", type=Path)
    ap.add_argument("--output-dir", type=Path, default=Path("specs_out"))
    ap.add_argument(
        "--prompt-file",
        type=Path,
        default=Path(__file__).resolve().parent / "ExtractSpec.txt",
    )
    ap.add_argument("--schema-file", type=Path)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--parallelism", type=int, default=DEFAULT_PARALLELISM)
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--start-row", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.csv.is_file():
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 2
    if not args.prompt_file.is_file():
        print(f"error: prompt file not found: {args.prompt_file}", file=sys.stderr)
        return 2
    if not args.dry_run and not (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ):
        print(
            "error: set GEMINI_API_KEY (https://aistudio.google.com/apikey) or use --dry-run.",
            file=sys.stderr,
        )
        return 2

    _, failures = process_csv(
        csv_path=args.csv,
        prompt_file=args.prompt_file,
        schema_file=args.schema_file,
        output_dir=args.output_dir,
        model=args.model,
        parallelism=args.parallelism,
        retries=args.retries,
        temperature=args.temperature,
        limit=args.limit,
        start_row=args.start_row,
        dry_run=args.dry_run,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
