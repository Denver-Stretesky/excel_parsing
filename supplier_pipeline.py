#!/usr/bin/env python3
"""
supplier_pipeline.py
====================

Take a supplier xlsx/csv, generate spec JSONs via csv_to_specs.py, then link
each spec to the matching unmatched SKU on the given supplier.

Usage
-----
    python supplier_pipeline.py path/to/file.xlsx --supplier-id <UUID>

Matching key is the supplier's own SKU code (== Cerve `supplier_sku_id`).
Auto-detects the column from common names; pass --sku-column to override.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

HERE = Path(__file__).resolve().parent
PROMPT_FILE = HERE / "ExtractSpec.txt"
SCHEMA_FILE = HERE / "spec_schema.json"
RUNNER_CLI = HERE / "csv_to_specs_cli.py"
RUNNER_API = HERE / "csv_to_specs.py"

AUTH_URL = "https://auth.cerve.com/v2/token"
SPEC_URL = "https://suppliers.cerve.com/v2/specs"
SUPPLIERS_BASE = "https://suppliers.cerve.com/v2/suppliers"

SKU_COL_CANDIDATES = [
    "supplier_sku_id", "SKU ID*", "Supplier Code", "Product Code",
    "RY Code", "SKU code", "sku_id", "sku"
]


# ---------------------------------------------------------------------------
# File reading (csv / xlsx / xls -> normalised csv)
# ---------------------------------------------------------------------------
def stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_to_csv(src: Path, dst: Path) -> tuple[list[str], list[dict[str, str]]]:
    ext = src.suffix.lower()
    if ext == ".csv":
        with src.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = [(h or "").strip() for h in (reader.fieldnames or [])]
            rows = [{(k or "").strip(): stringify(v) for k, v in r.items()} for r in reader]
    elif ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows(values_only=True)
        headers_raw = next((r for r in it if sum(1 for v in r if stringify(v)) >= 2), None)
        if not headers_raw:
            raise SystemExit("no header row found in spreadsheet")
        headers = [(stringify(h) or "").strip() for h in headers_raw]
        rows = [{headers[i]: stringify(v) for i, v in enumerate(r) if i < len(headers)} for r in it]
    elif ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(str(src))
        ws = wb.sheets()[0]
        header_idx = next(i for i in range(ws.nrows) if any(stringify(v) for v in ws.row_values(i)))
        headers = [(stringify(v) or "").strip() for v in ws.row_values(header_idx)]
        rows = [
            {headers[j]: stringify(v) for j, v in enumerate(ws.row_values(i)) if j < len(headers)}
            for i in range(header_idx + 1, ws.nrows)
        ]
    else:
        raise SystemExit(f"unsupported file type: {ext}")

    # Drop blank rows + section dividers (single ALL-CAPS cell, no digits).
    def keep(row: dict[str, str]) -> bool:
        vals = [v for v in row.values() if v]
        if not vals:
            return False
        if len(vals) == 1 and vals[0].isupper() and not any(c.isdigit() for c in vals[0]):
            return False
        return True

    kept = [r for r in rows if keep(r)]

    with dst.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)

    print(f"[READ] {len(rows)} rows -> {len(kept)} kept -> {dst}")
    return headers, kept


def find_sku_column(headers: list[str], explicit: str | None) -> str:
    if explicit:
        if explicit not in headers:
            raise SystemExit(f"column '{explicit}' not in headers: {headers}")
        return explicit
    lower = {h.lower(): h for h in headers}
    for c in SKU_COL_CANDIDATES:
        if c.lower() in lower:
            return lower[c.lower()]
    raise SystemExit(f"could not auto-detect SKU code column; pass --sku-column. headers: {headers}")


# ---------------------------------------------------------------------------
# Spec generation (delegate to csv_to_specs(_cli).py)
# ---------------------------------------------------------------------------
def generate_specs(csv_path: Path, out_dir: Path, use_api: bool) -> None:
    runner = RUNNER_API if use_api else RUNNER_CLI
    if not runner.is_file():
        raise SystemExit(f"runner not found: {runner}")
    cmd = [
        "uv", "run", str(runner), str(csv_path),
        "--prompt-file", str(PROMPT_FILE),
        "--schema-file", str(SCHEMA_FILE),
        "--output-dir", str(out_dir),
    ]
    print(f"[SPEC GEN] {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(HERE), check=False)
    if r.returncode != 0:
        raise SystemExit(f"spec generation failed (exit {r.returncode})")


# ---------------------------------------------------------------------------
# Cerve client
# ---------------------------------------------------------------------------
class CerveClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id, self.client_secret = client_id, client_secret
        self.token = self._auth()

    def _auth(self) -> str:
        r = requests.post(
            AUTH_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=60,
        )
        r.raise_for_status()
        print("[AUTH] ok")
        return r.json()["access_token"]

    def request(self, method: str, url: str, **kw) -> requests.Response:
        headers = dict(kw.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.token}"
        kw.setdefault("timeout", 60)
        r = requests.request(method, url, headers=headers, **kw)
        if r.status_code == 401:
            print("[AUTH] 401 — refreshing")
            self.token = self._auth()
            headers["Authorization"] = f"Bearer {self.token}"
            r = requests.request(method, url, headers=headers, **kw)
        return r


def get_credentials() -> tuple[str, str]:
    cid = os.environ.get("CERVE_CLIENT_ID")
    sec = os.environ.get("CERVE_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    sys.path.insert(0, str(HERE))
    from Authentication import AuthenticationRequirements  # type: ignore
    return AuthenticationRequirements.CLIENT_ID, AuthenticationRequirements.SECRET


# ---------------------------------------------------------------------------
# Cerve actions
# ---------------------------------------------------------------------------
def list_unmatched_skus(client: CerveClient, supplier_id: str) -> dict[str, str]:
    """Return {supplier_sku_id: cerve_sku_id} for SKUs with no spec linked."""
    url = f"{SUPPLIERS_BASE}/{supplier_id}/skus"
    out: dict[str, str] = {}
    token = None
    while True:
        params = {"page_size": "1000", "spec_id__is_null": "true"}
        if token:
            params["page_token"] = token
        r = client.request("GET", url, params=params)
        if r.status_code != 200:
            raise SystemExit(f"list SKUs failed: {r.status_code} {r.text[:300]}")
        body = r.json()
        for sku in body.get("skus") or []:
            sid, cid = sku.get("supplier_sku_id"), sku.get("id")
            if sid and cid:
                out[sid] = cid
        token = body.get("next_page_token")
        if not token:
            break
    print(f"[SKU LIST] {len(out)} unmatched SKUs on supplier")
    return out


def post_spec(client: CerveClient, spec: dict) -> str | None:
    r = client.request("POST", SPEC_URL,
                       headers={"Content-Type": "application/json"}, json=spec)
    if r.status_code in (200, 201):
        return r.json().get("id")
    if r.status_code == 409:
        # Duplicate by GTIN — find the existing one so we can still link.
        meta = (r.json().get("details") or [{}])[0].get("metadata") or {}
        gtin = meta.get("ean_13") or meta.get("upc_a") or meta.get("ean_8")
        if not gtin:
            print(f"[SPEC] 409 with no gtin: {r.text[:200]}")
            return None
        look = client.request("GET", SPEC_URL, params={"page_size": 1, "query": gtin})
        if look.status_code == 200:
            specs = look.json().get("specs") or []
            if specs:
                return specs[0]["id"]
        print(f"[SPEC] 409 lookup empty for gtin {gtin}")
        return None
    print(f"[SPEC] POST failed {r.status_code}: {r.text[:300]}")
    return None


def link_sku(client: CerveClient, supplier_id: str, sku_id: str, spec_id: str) -> bool:
    r = client.request(
        "PATCH", f"{SUPPLIERS_BASE}/{supplier_id}/skus/{sku_id}",
        headers={"Content-Type": "application/json"},
        json={"spec_id": spec_id},
    )
    if r.status_code != 200:
        print(f"[LINK] {sku_id} failed {r.status_code}: {r.text[:200]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("supplier_file", type=Path, help="xlsx, xls, or csv")
    ap.add_argument("--supplier-id", required=True, help="Cerve supplier UUID")
    ap.add_argument("--sku-column", help="Override auto-detection of supplier SKU code column")
    ap.add_argument("--use-api", action="store_true",
                    help="Use csv_to_specs.py (needs ANTHROPIC_API_KEY); default is the CLI runner")
    args = ap.parse_args()

    if not args.supplier_file.is_file():
        raise SystemExit(f"file not found: {args.supplier_file}")

    out_dir = HERE / "specs_out" / args.supplier_file.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    normalised_csv = out_dir / "normalised.csv"

    # 1. Read + normalise
    headers, rows = read_to_csv(args.supplier_file, normalised_csv)
    sku_col = find_sku_column(headers, args.sku_column)
    print(f"[COLS] using sku column '{sku_col}'")

    # 2. Generate spec JSONs (row_1.json ... row_N.json), then rename to <sku_id>.json
    generate_specs(normalised_csv, out_dir, args.use_api)
    for idx, row in enumerate(rows, start=1):
        sku_id = (row.get(sku_col) or "").strip()
        if not sku_id:
            continue
        for suffix in (".json", ".error.json"):
            src = out_dir / f"row_{idx}{suffix}"
            if src.is_file():
                src.rename(out_dir / f"{sku_id}{suffix}")

    # 3. Auth + list unmatched SKUs on the supplier
    client = CerveClient(*get_credentials())
    sku_lookup = list_unmatched_skus(client, args.supplier_id)

    # 4. For each row, POST spec, then PATCH the matching unmatched SKU.
    summary = {"linked": 0, "spec_only": 0, "no_match": 0, "no_spec": 0, "spec_failed": 0}
    for idx, row in enumerate(rows, start=1):
        supplier_sku_id = (row.get(sku_col) or "").strip()
        if not supplier_sku_id:
            print(f"[ROW {idx}] no sku id in row — skip")
            summary["no_spec"] += 1
            continue
        spec_path = out_dir / f"{supplier_sku_id}.json"
        if not spec_path.is_file():
            print(f"[ROW {idx}] no spec json for sku '{supplier_sku_id}' — skip")
            summary["no_spec"] += 1
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if not spec.get("name"):
            print(f"[ROW {idx}] empty spec (divider?) — skip")
            summary["no_spec"] += 1
            continue

        spec_id = post_spec(client, spec)
        if not spec_id:
            summary["spec_failed"] += 1
            continue

        cerve_sku_id = sku_lookup.get(supplier_sku_id)
        if not cerve_sku_id:
            print(f"[ROW {idx}] spec {spec_id} created, no unmatched SKU for '{supplier_sku_id}'")
            summary["no_match"] += 1
            continue

        if link_sku(client, args.supplier_id, cerve_sku_id, spec_id):
            print(f"[ROW {idx}] linked {cerve_sku_id} -> {spec_id}")
            summary["linked"] += 1
        else:
            summary["spec_only"] += 1

    print(f"[DONE] {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
