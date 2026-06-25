"""
spec_cleanup.py
===============

Pre-POST cleanup of a Cerve spec dict — ported from Automation/JSON_Cleanup.py
so the desktop pipeline applies the same normalisations the prior Cerve uploader
relied on. Called from supplier_pipeline.main() right after loading the spec
JSON, before validate_spec / post_spec.

Notable normalisations:
- unit_of_measure: default "item" if null (Cerve POST rejects null)
- manufacturing_country_codes / ingredient origin_country_codes:
    [] / [None] / ["unknown"] → null;  "EU" → all 27 EU country codes
- volume.unit: "L" / "Litre" / "litre" → "l"
- references: if all three (EAN_13/EAN_8/UPC_A) null → references = null
- logistics.physical_properties.packaging.dimensions: if any dimension missing,
    drop dimensions entirely; if material has no name+weight, drop material;
    if type+dimensions+material all null, drop packaging
- product weight/volume: drop any whose value is null
- nutrition: drop entries with no serving; normalise energy/fat/carb units;
    µg/mcg/ug → mcg

If cleanup throws (e.g. unexpected shape), the caller falls back to the raw
spec — better to attempt the POST than to lose the row.
"""
from __future__ import annotations

_EU_COUNTRIES = [
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE",
]

# Canonical content_declarations list for every spec. Price-list files almost
# never carry allergen/additive data, but Cerve still wants the full template
# present — we emit all 30 entries with status "unknown" and let any explicit
# detections from the model override.
_ALLERGENS = [
    "wheat", "barley", "oats", "rye", "gluten", "crustaceans", "eggs", "fish",
    "peanuts", "soybeans", "milk", "celery", "mustard", "sesame",
    "sulphur-dioxide", "sulphites", "lupin", "molluscs", "almonds",
    "hazelnuts", "cashews", "walnuts", "pecan-nuts", "brazil-nuts",
    "pistachio-nuts", "macadamia-nuts",
]
_ADDITIVES = [
    "artificial-flavours", "artificial-flavourings", "artificial-colours",
    "artificial-antioxidants", "artificial-preservatives", "artificial-sweeteners",
]
_ALLERGEN_STATUSES = ("yes", "no", "may-contain", "unknown")


def _default_content_declarations() -> list[dict]:
    out = []
    for a in _ALLERGENS:
        out.append({"name": a, "type": "allergen", "status": "unknown", "quantity": None})
    for a in _ADDITIVES:
        out.append({"name": a, "type": "additive", "status": "unknown", "quantity": None})
    return out


def _expand_eu(codes: list) -> list:
    if codes and "EU" in codes:
        codes = [c for c in codes if c != "EU"]
        codes.extend(_EU_COUNTRIES)
    return codes


def _gtin_check_digit_valid(code, expected_length: int) -> bool:
    """GS1 mod-10 check digit validation for GTIN-8/12/13.

    Algorithm: sum the first N-1 digits with alternating weights of 3 and 1
    starting from the rightmost body digit. The check digit (last position)
    must equal (10 - sum % 10) % 10. Returns False for the wrong length, any
    non-digit characters, or a mismatched check digit."""
    if code is None:
        return False
    s = str(code).strip()
    if not s.isdigit() or len(s) != expected_length:
        return False
    digits = [int(c) for c in s]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return check == (10 - total % 10) % 10


_GTIN_SLOTS = (("EAN_13", 13), ("UPC_A", 12), ("EAN_8", 8))


def _recover_gtin(raw) -> tuple[str, str] | None:
    """Try to find a valid (slot, value) for a barcode string. Returns
    ``(slot, normalised_value)`` if any of these variants validates against
    GS1 mod-10, else None:

      1. The raw value as-is.
      2. The value with a leading zero prepended (Excel strips leading zeros
         when storing barcode columns as numbers — an 11-digit `71570000356`
         is really `071570000356` UPC-A).
      3. The value with the leading digit stripped, but ONLY if the value is
         14 digits AND that digit is `0` (a GTIN-14 with indicator `0` has the
         consumer EAN-13 as its trailing 13 digits)."""
    if raw is None:
        return None
    s = str(raw).strip().lstrip("'")
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit():
        return None
    candidates = [s]
    if len(s) in (7, 11, 12):  # one short of 8, 12, 13 — Excel-stripped leading zero
        candidates.append("0" + s)
    if len(s) == 14 and s.startswith("0"):  # GTIN-14 with indicator 0 → strip to EAN-13
        candidates.append(s[1:])
    for c in candidates:
        for slot, length in _GTIN_SLOTS:
            if len(c) == length and _gtin_check_digit_valid(c, length):
                return slot, c
    return None


def clean_spec(data: dict) -> tuple[dict, list[dict]]:  # noqa: C901 — single-function port, matches JSON_Cleanup.py
    """Returns (cleaned_data, dropped_gtins) where dropped_gtins is a list of
    ``{"field": "EAN_13"|"UPC_A"|"EAN_8", "value": "<raw>"}`` for every barcode
    that failed mod-10 check-digit validation and was nulled. The caller can
    accumulate these per-row to build a CSV of GTINs the user should fix."""
    dropped_gtins: list[dict] = []
    # manufacturers
    if data.get("manufacturers") in ([], [None]):
        data["manufacturers"] = None

    # manufacturing country codes
    if data.get("manufacturing_country_codes") in ([], [None]):
        data["manufacturing_country_codes"] = None
    if data.get("manufacturing_country_codes") is not None:
        data["manufacturing_country_codes"] = _expand_eu(data["manufacturing_country_codes"])

    # unit_of_measure must be non-null
    if data.get("unit_of_measure") is None:
        data["unit_of_measure"] = "item"

    # ingredients
    if data.get("ingredients") is not None:
        for ingredient in data["ingredients"]:
            occ = ingredient.get("origin_country_codes")
            if isinstance(occ, str):
                ingredient["origin_country_codes"] = [occ]
                occ = ingredient["origin_country_codes"]
            if occ in (["unknown"], [], [None]):
                ingredient["origin_country_codes"] = None
            elif occ is not None:
                ingredient["origin_country_codes"] = _expand_eu(occ)
        data["ingredients"] = [i for i in data["ingredients"] if i.get("name") is not None] or None
    if data.get("ingredients") == [None]:
        data["ingredients"] = None

    # dietary suitability
    if data.get("dietary_suitability") is not None:
        for diet in data["dietary_suitability"]:
            if diet.get("status") is None:
                diet["name"] = None
            elif diet["status"] == "suitable" or diet["status"] == "True":
                diet["status"] = "true"
            elif diet["status"] == "False":
                diet["status"] = "false"
        data["dietary_suitability"] = [d for d in data["dietary_suitability"] if d.get("name") is not None]
        if not data["dietary_suitability"]:
            data["dietary_suitability"] = None

    # price-marked packs
    if data.get("price_marked_packs") is not None:
        for pmp in data["price_marked_packs"]:
            if pmp.get("quantity") is None:
                data["price_marked_packs"] = None
                break
    if data.get("price_marked_packs") == []:
        data["price_marked_packs"] = None

    # content declarations: always emit the full canonical list (every allergen
    # and additive) defaulted to "unknown". Any explicit entries the model
    # returned overlay the template by name, so a detected wheat=yes still wins.
    template = _default_content_declarations()
    by_name = {entry["name"]: entry for entry in template}
    for model_entry in (data.get("content_declarations") or []):
        if not isinstance(model_entry, dict):
            continue
        name = (model_entry.get("name") or "").lower()
        if name not in by_name:
            continue  # ignore non-canonical / hallucinated names
        status = model_entry.get("status")
        if status in _ALLERGEN_STATUSES:
            by_name[name]["status"] = status
        q = model_entry.get("quantity")
        if q is not None and q.get("unit") is not None:
            by_name[name]["quantity"] = q
    # Sulphites/sulphur-dioxide always get the EU 10ppm threshold quantity.
    for entry in by_name.values():
        if entry["name"] in ("sulphites", "sulphur-dioxide"):
            entry["quantity"] = {"unit": "ppm", "value": 10, "comparison": "greaterThan"}
    data["content_declarations"] = list(by_name.values())

    # sub-specs
    if data.get("sub_specs") is not None:
        for subspec in data["sub_specs"]:
            if subspec is not None and subspec.get("quantity") is None:
                data["sub_specs"] = None
                break
        if data.get("sub_specs") == []:
            data["sub_specs"] = None

    # references: validate GTIN check digits with leading-zero / GTIN-14 recovery.
    # For each slot, try as-is, then a 0-padded variant (Excel strips leading
    # zeros), then a leading-zero-stripped variant for 14-digit codes. Any
    # variant that validates is placed in its correct slot. Codes with no
    # valid form are dropped + logged so the spec can still POST without them.
    refs = data.get("references")
    if refs is not None:
        recovered = {"EAN_13": None, "UPC_A": None, "EAN_8": None}
        for orig_slot in ("EAN_13", "UPC_A", "EAN_8"):
            raw = refs.get(orig_slot)
            if raw is None:
                continue
            rec = _recover_gtin(raw)
            if rec is None:
                print(f"[CLEANUP] dropping invalid {orig_slot}: {raw!r} (no valid GTIN form)")
                dropped_gtins.append({"field": orig_slot, "value": str(raw)})
                continue
            new_slot, new_value = rec
            if recovered[new_slot] is not None and recovered[new_slot] != new_value:
                # destination already holds a different recovered value — keep first, log the loser
                print(f"[CLEANUP] {orig_slot} {raw!r} → {new_slot} collides; dropping")
                dropped_gtins.append({"field": orig_slot, "value": str(raw)})
                continue
            if new_slot != orig_slot or new_value != str(raw).strip().lstrip("'").rstrip("0").rstrip("."):
                # Only log when something actually changed (slot or value).
                if str(raw).strip() != new_value or new_slot != orig_slot:
                    print(f"[CLEANUP] recovered {orig_slot} {raw!r} → {new_slot} {new_value!r}")
            recovered[new_slot] = new_value
        if all(v is None for v in recovered.values()):
            data["references"] = None
        else:
            data["references"] = recovered

    # logistics
    log = data.get("logistics")
    if log is not None:
        # storage: drop entries that have nothing populated
        if log.get("storage") is not None:
            for storage in log["storage"]:
                if all(storage.get(k) is None for k in (
                    "temperature_min_celsius", "temperature_max_celsius",
                    "conditions", "shelf_life_days", "hours_after_opening",
                )):
                    log["storage"] = None
                    break

        pp = log.get("physical_properties")
        if pp is not None:
            # inner gross weight
            igw = pp.get("inner_gross_weight")
            if igw is not None and igw.get("value") is None:
                pp["inner_gross_weight"] = None

            # packaging
            pk = pp.get("packaging")
            if pk is not None:
                dims = pk.get("dimensions")
                if dims is not None:
                    for axis in ("height", "width", "length"):
                        d = dims.get(axis)
                        if d is not None and d.get("value") is None:
                            dims[axis] = None
                    if any(dims.get(a) is None for a in ("height", "width", "length")):
                        pk["dimensions"] = None
                mat = pk.get("material")
                if mat is not None:
                    for m in mat:
                        if m.get("name") is not None and m.get("weight") is not None \
                                and m["weight"].get("unit") is None:
                            m["weight"] = None
                    pk["material"] = [
                        m for m in mat
                        if m.get("name") is not None
                        or (m.get("weight") and m["weight"].get("value") is not None)
                    ]
                    if not pk["material"]:
                        pk["material"] = None
                if pk.get("type") is None and pk.get("dimensions") is None and pk.get("material") is None:
                    pp["packaging"] = None

            # product: Cerve requires the `weight` and `volume` keys to be
            # PRESENT inside `product` (each may be null), and `weight` if
            # present must have `inner_net` and `drained` sub-keys (each may
            # be null). Missing structural keys → "weight: field required" 400.
            prod = pp.get("product")
            if prod is not None:
                w = prod.get("weight")
                if w is not None:
                    if w.get("inner_net") is not None and w["inner_net"].get("value") is None:
                        w["inner_net"] = None
                    if w.get("drained") is not None and (
                        w["drained"].get("value") is None or w["drained"].get("unit") is None
                    ):
                        w["drained"] = None
                    # Ensure both structural sub-keys exist
                    w.setdefault("inner_net", None)
                    w.setdefault("drained", None)
                vol = prod.get("volume")
                if vol is not None:
                    if vol.get("unit") in ("L", "Litre", "litre"):
                        vol["unit"] = "l"
                    if vol.get("value") is None:
                        prod["volume"] = None
                # Ensure both structural keys exist even when null
                prod.setdefault("weight", None)
                prod.setdefault("volume", None)
                # If both are entirely empty, collapse the whole product
                if prod.get("weight") is None and prod.get("volume") is None:
                    pp["product"] = None

    # nutrition
    if data.get("nutrition") is not None:
        for ix in range(len(data["nutrition"])):
            entry = data["nutrition"][ix]
            if entry is None:
                continue
            serving = entry.get("serving") or {}
            if serving.get("value") is None and serving.get("unit") is None:
                data["nutrition"][ix] = None
                continue

            energy = entry.get("energy")
            if energy is not None:
                cal = energy.get("calories")
                if cal is not None and cal.get("unit") in ("Kcal", "cal"):
                    cal["unit"] = "kcal"
                kj = energy.get("kilojoules")
                if kj is not None and kj.get("unit") == "kj":
                    kj["unit"] = "kJ"

            fat = entry.get("fat")
            if fat is not None:
                for k in ("unsaturated", "monounsaturated", "polyunsaturated"):
                    f = fat.get(k)
                    if f is not None and f.get("value") is None:
                        fat[k] = None

            carbs = entry.get("carbohydrates")
            if carbs is not None:
                for k in ("starch", "polyols"):
                    c = carbs.get(k)
                    if c is not None and c.get("value") is None:
                        carbs[k] = None

            fibre = entry.get("fibre")
            if fibre is not None and (
                fibre.get("value") is None or fibre.get("comparison") is None or fibre.get("unit") is None
            ):
                entry["fibre"] = None

            vit = entry.get("vitamins")
            if vit is not None:
                vit = {k: v for k, v in vit.items() if v is not None and v.get("value") is not None}
                if vit:
                    for v in vit.values():
                        if v.get("unit") in ("µg", "mcg", "ug"):
                            v["unit"] = "mcg"
                entry["vitamins"] = vit or None

            mins = entry.get("minerals")
            if mins is not None:
                mins = {k: v for k, v in mins.items() if v is not None and v.get("value") is not None}
                entry["minerals"] = mins or None

        if data["nutrition"] == [None]:
            data["nutrition"] = None

    return data, dropped_gtins
