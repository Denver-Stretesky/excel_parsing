# Changelog

## 0.7.5 — PMP extraction made deterministic
- New dedicated `## price_marked_packs` section in the prompt. The model now has explicit positive + negative rules instead of inferring row-by-row:
  - **Source**: extract from the product description / name field only — never from `RRP` / `RSP` / `List Price` / `Cost` / `WSP` / other trade-price columns.
  - **Trigger**: a `PMP` keyword adjacent to a price (`£X.XX PMP`, `PMP £X.XX`, `PMP @ £X.XX`, `(£X.XX PMP)`, `Price-Marked` + amount).
  - **`NON PMP`** in the description → emit `null` (explicit non-PMP).
  - **PMP word with no on-pack price visible** → emit `null` (can't fabricate).
  - **Multi-buy** PMPs supported: `Buy 2 for £3 PMP` → `{amount: 3.00, quantity: 2}`.
  - Currency defaults to `GBP`; `$` → `USD`, `€` → `EUR`.
- Fixes the 40%-hit-rate inconsistency where some rows had `price_marked_packs` extracted from an RRP column and others didn't.

## 0.7.4 — GTIN recovery + single keychain prompt
- **Inner-GTIN recovery**: before dropping a barcode whose GS1 check digit fails, the cleaner now tries up to two recoveries:
  1. **Leading-zero pad** — Excel routinely stores barcode columns as numbers and strips leading zeros, so an 11-digit `71570000356` is really `071570000356` (UPC-A). Pad and re-validate.
  2. **GTIN-14 with indicator `0`** — a 14-digit code that starts with `0` is a GTIN-14 padded from an EAN-13. Strip the leading zero and re-validate. (We do NOT strip non-zero leading digits — those are real indicators and the trailing 13 digits aren't a valid consumer EAN-13.)
- Recovered codes are placed in whichever slot they actually validate as (EAN_13 / UPC_A / EAN_8), not just kept in the original column. `[CLEANUP] recovered <orig> → <new_slot> <new_value>` is logged when something changes.
- Net effect on real files: the Fine Confectionery file went from 23 dropped GTINs to 0 — everything was recoverable.

- **Settings: one OS keychain prompt instead of three** — all three credentials are now stored as a single JSON blob under one keychain item, and the in-process value is cached, so opening the app prompts at most once per launch (used to be once per credential).

## 0.7.3 — Model upgrade
- Default Gemini model bumped from `gemini-2.5-flash` to `gemini-3.5-flash`. Better at structured extraction and pattern recognition (sub-spec detection, abbreviation decoding) at similar latency.

## 0.7.2 — Full allergen + additive template
- Every spec now emits the canonical `content_declarations` list — 26 allergens + 6 additives — defaulted to `status: "unknown"`. Price-list files rarely carry allergen info, so the full template is filled in locally rather than left null.
- Any explicit detections from the model (e.g. `wheat: yes` from a "Contains Wheat" cell) **overlay** the defaults, so real signals still win.
- Sulphites and sulphur-dioxide automatically get the EU regulatory `{unit: "ppm", value: 10, comparison: "greaterThan"}` quantity.
- Non-canonical / hallucinated allergen names returned by the model are silently dropped.

## 0.7.1 — Broader sub-spec pattern recognition
- Prompt updated so the model recognises two-level packs across all common layouts seen in supplier files:
  - Reverse-order strings (`25g x 6 x 8`, `20g x 5 x 10`) — same meaning as `8x6x25g`/`10x5x20g`.
  - Split-column form: a pack-size cell `M x V<unit>` (e.g. `6 x 25g`) plus a separate "Primary Packs per Case" / "per case" column with N.
  - Range/category-name hints (`"Crinkle 6 Pack"`) used as confirmation that the consumer pack contains M children.
- New negative examples spell out when *not* to use sub_specs: a standalone weight column (e.g. `84g`) with a separate per-case count is still a single-level multi-pack.

## 0.7.0 — Sub-spec support for two-level packs
- **New pattern recognised**: quantity columns of the form `N x M x V<unit>` (e.g., `10x5x20g`, `12x6x330ml`) now produce a parent + child spec pair instead of a single multi-pack.
  - **Child spec** = one individual unit (the 20g item, the 330ml can).
  - **Parent spec** = the consumer pack containing M children; references the child via `sub_specs: [{spec_id, quantity: M}]`.
  - Outer N (case multiplier) is treated as shipping, not consumer — same as before.
- Pipeline now creates child specs **before** the parent: each `_inline_spec` marker is cleaned, validated, POSTed (or reused via 409), and the returned `spec_id` is substituted before the parent goes out.
- Single-level multi-packs (`12x500g`, `24x330ml`) still use the existing weight/volume rules — no sub_specs.
- GTINs dropped during child cleanup show up in `dropped_gtins.csv` as `<sku> (child)` so you can tell them apart from parent-level failures.

## 0.6.6 — Invalid GTIN handling
- **GTIN check-digit validation**: every EAN-13 / UPC-A / EAN-8 in `references` is now validated locally with the GS1 mod-10 algorithm before POST. Invalid barcodes are nulled out so the spec still uploads.
- **`dropped_gtins.csv`** written to the spec output dir at end of each run, listing every SKU whose GTIN was dropped (`supplier_sku_id, spec_name, field, value`) — hand back to the supplier for source-data correction.

## 0.6.5 — Correct null handling for nested structural fields
- `_strip_nulls` is now **shallow** (top-level only). Nested nulls survive — required because Cerve rejects top-level `content_declarations: null` but *requires* nested keys like `product.weight.inner_net` to be explicitly null when there's no value.
- `spec_cleanup` now uses `setdefault(..., None)` to guarantee `product.weight` / `product.volume` keys exist and `weight.inner_net` / `weight.drained` sub-keys exist, fixing the recurring `weight: field required` 400s on volume-only liquids (Heinz, Daddies, Lea & Perrins).

## 0.6.4 — Empty-object pruning (later refined in 0.6.5)
- `_strip_nulls` extended to drop empty dicts/lists after null removal. Helped some cases but caused regressions for liquids — fully superseded by 0.6.5.

## 0.6.3 — Spec cleanup ported from Automation
- New `spec_cleanup.py` module — port of `JSON_Cleanup.py` adapted for the desktop pipeline.
- Normalisations applied pre-POST:
  - `unit_of_measure` defaults to `"item"` if null
  - `"EU"` in country codes expands to all 27 EU codes
  - Volume unit normalised: `L` / `Litre` / `litre` → `l`
  - `references` collapsed to null when all three barcode slots are null
  - Packaging dimensions / material pruned when incomplete
  - Nutrition vitamin units normalised (`µg` / `ug` → `mcg`)

## 0.6.2 — 400 error visibility
- Failed `POST /v2/specs` calls now log with row context: `[ROW N] POST 400 for '<name>' (sku <id>) — <field>: <reason>`.
- Full payload + raw response saved to `<sku>.post_failed.json` in the spec dir for inspection.
- Cerve's `metadata` field is parsed out so you see exactly which field broke.

## 0.6.1 — Live per-row output
- Spec-generation output now streams as each row finishes (no more long wait for one big batch at the end).
- Each line includes: row N/total, elapsed time, extracted `name` / `brand` / `unit_of_measure`, plus the consumer barcode if extracted.
- Failed rows show their error inline with timing.

## 0.6.0 — Local spec validation
- New `validate_spec()` runs before every POST:
  - Explicit non-null checks for `name` and `unit_of_measure`.
  - Full nested-shape validation against `spec_schema.json` (catches wrong types, malformed structures, bad enums at any depth).
- Invalid specs are logged with the exact failing path and never reach Cerve.

## 0.5.2 — Header detection for messy files
- Header row picked by **keyword density** (look for `code`, `name`, `barcode`, `ean`, `sku`, etc. in the first 30 rows) instead of just "first row with ≥2 populated cells". Handles files with title/banner rows before the real header (FBC, P13).
- Trailing empty-header columns auto-pruned (fixes the 56-column FBC and 63-column P13 noise that polluted the SKU dropdown).
- Section/divider rows (`SPECIAL LAUNCH DEAL`, `WALKERS NON PMP @ £15.35`, etc.) now filtered regardless of casing/digits — any row with fewer than 2 populated cells is dropped.

## 0.5.1 — Run-all UX
- `Run all` button is automatically disabled when the queue is empty so accidental clicks no longer print `[queue] all done` against nothing.

## 0.5.0 — Multi-file queue with mid-run additions
- Replaced the single-file "Run" button with `+ Add to queue` and `Run all`.
- Each queued item shows file / supplier / SKU-column with a remove (✕) button.
- New jobs can be added **while a run is in progress** — the worker picks them up automatically. If the queue stays empty for 5 seconds the worker exits; clicking Run all restarts it.
- Per-item start/end markers (`=== <file> → supplier <id> (col '<x>') ===`, `--- <file> finished OK ---`) make multi-file output easy to follow.
- Final `[queue] all done.` / `[queue] worker exited; click Run all to resume.` lets you know whether a run was complete or worker-died.

## 0.4.0 — Credentials moved to OS keychain
- Cerve client ID/secret and Gemini API key now stored in the OS-native secure store (macOS Keychain on Mac, Credential Manager on Windows, Secret Service on Linux) via the `keyring` library.
- Plaintext `config.json` is gone for credentials; the only thing in the app-data dir is the non-sensitive `sku_columns.json`.
- One-time auto-migration: an existing `config.json` from 0.3.x or earlier has its creds moved into the keychain on first launch and the file is deleted.
- Settings panel now reads "Stored securely in your system keychain".
