# excel_parsing

Turn a supplier price/promotion file (xlsx, xls, csv) into Cerve product specs
and link each one to the supplier's matching unmatched SKU.

Two front ends over the same pipeline:

- **SupplierPipeline.app** — the macOS desktop app (`app.py`), what gets handed
  to non-developers. Built with PyInstaller.
- **`supplier_pipeline.py`** — the same pipeline on the command line.

Extraction runs on Gemini, in-process (no subprocess), so the whole thing can be
frozen into a bundle.

## Setup

```bash
# inside excel_parsing/
uv sync                                 # creates .venv, installs deps
export GEMINI_API_KEY=...                # https://aistudio.google.com/apikey
export CERVE_CLIENT_ID=...
export CERVE_CLIENT_SECRET=...
```

Cerve credentials fall back to `Authentication.py` (`AuthenticationRequirements
.CLIENT_ID` / `.SECRET`) if the env vars aren't set. `GOOGLE_API_KEY` works in
place of `GEMINI_API_KEY`.

## Run (CLI)

```bash
uv run supplier_pipeline.py "Examples/pepsico.csv" --supplier-id <UUID>
```

- `--supplier-id` — Cerve supplier UUID (required).
- `--sku-column "Product Code"` — override auto-detection of the column holding
  the supplier's own SKU code. That column is the join key: it must match
  `supplier_sku_id` on the Cerve side.

Output goes to `specs_out/<file stem>/`.

## Run (app)

```bash
uv run app.py
```

Settings (Cerve client id + secret, Gemini API key) are stored in the macOS
keychain as one JSON blob under service `SupplierPipeline`, so you get at most
one keychain prompt per launch. The settings screen opens by itself on first
run. Pick a file, confirm the auto-detected SKU column, paste the supplier UUID,
and queue it. The app writes to `~/Documents/SupplierPipeline/specs_out/<file
stem>/` — not next to the binary, which is read-only inside a bundle.

## What a run does

1. **Read** the file and find the header row (see below), writing the cleaned
   result to `normalised.csv`.
2. **List unmatched SKUs** on the supplier (`spec_id__is_null`) and **keep only
   rows whose SKU code appears there** — so rows that already have a spec never
   reach the model. `[FILTER] N rows -> M match an unmatched SKU`.
3. **Extract** one spec per row via Gemini (`ExtractSpec.txt` as the prompt,
   `spec_schema.json` as the shape), 4 rows in parallel, written as
   `row_N.json` then renamed to `<sku>.json`.
4. **Clean** each spec (`spec_cleanup.py`): GTIN check digits, unit
   normalisation, allergen/additive template, EU country expansion.
5. **Validate** against the schema locally, with `required` stripped — Cerve
   enforces required fields itself, and marks fields required that are legitimately
   null at create time.
6. **POST** the spec, then **PATCH** the SKU to link it. Two-level packs
   (`10x5x20g`) get their child spec created first so the parent can reference a
   real `spec_id`.

Files left in the output dir:

| file | what it is |
|---|---|
| `normalised.csv` | exactly what the model was fed |
| `<sku>.json` | the generated spec |
| `<sku>.error.json` | model returned something unusable |
| `<sku>.post_failed.json` | full payload + Cerve's response for a failed POST |
| `dropped_gtins.csv` | barcodes dropped for failing their check digit |

A run ends with a summary: `linked_to_new_spec`, `linked_to_existing_spec`
(matched by GTIN via 409, existing spec left untouched), `spec_only` (spec made,
link failed), `no_spec`, `spec_failed`.

## Reading messy supplier files

Price lists and promotion forms are laid out for humans, so the reader does not
assume row 1 is the header:

- **Header detection** (`_pick_header_row`) scores the first 30 rows on header
  keyword density and picks the best. Euro Food Brands' promo form has its real
  headers on **line 11**, under a logo block, contact details and period dates.
- **Unlabelled columns are kept**, named `Column N`. Suppliers often leave the
  product-name or SKU-code column with no header; only columns with neither a
  header nor any data are dropped.
- **Trailing blocks are trimmed** (`_trim_trailing_block`). Promotion forms
  append a disclaimer and a P1–P17 period calendar below the products; those
  rows fill different columns from the real data, so the reader cuts at the
  first run of 5 consecutive off-pattern rows. The 5-row buffer keeps section
  dividers (`MIGHTY OATS`) and blank lines from truncating a file early.

`Examples/` holds 16 real supplier files spanning these layouts — useful as a
regression set when touching the reader.

## Building the app

```bash
uv run pyinstaller app.spec --noconfirm
```

Produces `dist/SupplierPipeline.app` (distribute this) and
`dist/SupplierPipeline/` (unbundled, ignore). Bump `_version.py` first — it
feeds the window title, the bundle's `CFBundleShortVersionString`, and the zip
name below.

To zip for distribution:

```bash
V=$(uv run python -c "from _version import __version__; print(__version__)")
ditto -c -k --keepParent dist/SupplierPipeline.app "dist/SupplierPipeline-$V.app.zip"
```

Use `ditto`, not `zip -r`: it preserves the symlinks and extended attributes
that keep the bundle's signature intact.

The bundle is **ad-hoc signed** (`app.spec` sets no `codesign_identity`), so
Gatekeeper blocks it on another Mac on first open — right-click → Open, or
`xattr -dr com.apple.quarantine SupplierPipeline.app`. Proper distribution would
need a Developer ID identity plus notarization.

## Layout

| file | role |
|---|---|
| `app.py` | desktop UI (customtkinter): settings, file queue, log pane |
| `supplier_pipeline.py` | the pipeline — read, filter, extract, clean, validate, POST, link |
| `gemini_csv_to_specs.py` | per-row Gemini calls; also a standalone CLI |
| `spec_cleanup.py` | local spec normalisation + GTIN validation |
| `ExtractSpec.txt` | the extraction prompt |
| `spec_schema.json` | Cerve spec schema, used for local validation |
| `app.spec` | PyInstaller build spec |
| `_version.py` | single source of truth for the version |
| `CHANGELOG.md` | per-release notes |

`gemini_csv_to_specs.py` runs on its own for prompt work, without touching
Cerve:

```bash
uv run gemini_csv_to_specs.py normalised.csv --output-dir /tmp/specs --dry-run
```

`--dry-run` writes the would-be prompt per row instead of calling the API.
Also takes `--model`, `--parallelism`, `--retries`, `--temperature`, `--limit`,
`--start-row`, `--prompt-file`, `--schema-file`.

### Legacy

`csv_to_specs.py` (Anthropic API) and `csv_to_specs_cli.py` (the `claude -p`
CLI) are the original Claude-based runners, driven by `BatchCreateSpec.txt`.
Nothing in the app path uses them; they're kept for prompt experiments. Their
`anthropic` dependency sits in its own group, so it isn't installed by default:

```bash
uv sync --group legacy
export ANTHROPIC_API_KEY=sk-ant-...
```
