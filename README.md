# excel_parsing

Convert each row of a product CSV into a Cerve product-spec JSON, using Claude
and the rules in `BatchCreateSpec.txt`.

## Setup

```bash
# inside excel_parsing/
uv sync                         # creates .venv and installs anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
uv run csv_to_specs.py sample_products.csv \
    --output-dir specs_out \
    --schema-file cerve_spec_schema.json    # OR --schema-url https://...
```

The schema is optional. Without it, Claude is told to follow the field
structure described in `BatchCreateSpec.txt`. To use the live Cerve schema,
either download it once and pass `--schema-file`, or pass `--schema-url` and
the script fetches it at runtime.

## Useful flags

- `--dry-run` — write the would-be prompt for each row to
  `<output_dir>/row_NNNN.prompt.txt` instead of calling the API. Good for
  inspecting what gets sent without burning tokens.
- `--limit N` / `--start-row K` — process only a slice of the CSV.
- `--parallelism N` — concurrent API calls (default 4).
- `--model claude-sonnet-4-6` — override the model.

Per-row failures are written as `row_NNNN.error.json` (with the raw response
when the model returns non-JSON), and processing of other rows continues.
