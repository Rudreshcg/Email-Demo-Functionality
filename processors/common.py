from typing import Any, Dict, List, Tuple
from datetime import datetime


def coerce_number(value) -> float:
    try:
        if isinstance(value, str):
            v = value.replace(",", "").strip()
            return float(v) if v else 0.0
        return float(value)
    except Exception:
        return 0.0


def clean_material_code(material: str) -> str:
    if not material:
        return ""
    material = str(material).strip()
    if material in ("####", "#####", "-", "N/A", "n/a", "None", ""):
        return ""
    import re
    match = re.match(r"^(\d+)", material)
    if match:
        return match.group(1)
    return material


def sanitize_rows(
    rows: List[Dict[str, Any]],
    customer: str,
    source: str,
    source_file: str,
) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in rows or []:
        qty = coerce_number(item.get("quantity", 0))
        material = str(item.get("material", "") or item.get("id", "") or "").strip()
        if not material:
            continue
        material = clean_material_code(material)
        if not material:
            continue
        notes = str(item.get("notes", "") or "")
        description = str(item.get("description", "") or "")
        if description and notes:
            final_notes = f"{description} | {notes}"
        elif description:
            final_notes = description
        else:
            final_notes = notes
        if "dropped" in final_notes.lower():
            continue
        out = dict(item)
        out["material"] = material
        out["quantity"] = qty
        out["customer"] = out.get("customer") or customer or ""
        out["description"] = description
        out["notes"] = final_notes
        out["source"] = source
        out["source_file"] = source_file
        cleaned.append(out)
    return cleaned


def dedupe_requirements(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for row in rows:
        key = (
            row.get("source"),
            row.get("material"),
            row.get("delivery_date"),
            row.get("quantity"),
            row.get("unit"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped

