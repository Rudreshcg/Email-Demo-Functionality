import os
import re
from typing import Dict, List, Any

from analysis import _normalize_date_to_iso
from parsers.xlsx_parser import read_xlsx_bytes

from .common import clean_material_code, coerce_number, sanitize_rows


def extract_requirements_from_excel_row(
    rec: Dict[str, Any],
    bedrock,
    sender: str,
    customer: str,
    source: str,
    source_file: str,
    row_idx: int,
    debug: bool = False,
) -> List[Dict[str, Any]]:
    month_pattern = re.compile(
        r"^(\d+[-.])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$",
        re.IGNORECASE,
    )
    month_cols = []
    other_cols = {}
    for k, v in rec.items():
        header = str(k).strip()
        if month_pattern.match(header):
            month_cols.append((header, v if v == v else None))
        else:
            other_cols[header] = v

    id_candidates_priority = [
        "SKU",
        "Product Number",
        "Product ID",
        "Item Code",
        "Item Number",
        "Material",
        "Code",
        "Part #",
        "Part Number",
        "Supplier Part #",
        "Product Code",
        "Item",
    ]
    id_candidates_fallback = [
        "Item Description",
        "Item Descrip",
        "Description",
        "Product Description",
    ]
    id_candidates_lower_priority = ["Loc", "Location"]

    material = ""
    for col_name in id_candidates_priority:
        for header, value in other_cols.items():
            header_lower = header.lower()
            candidate_lower = col_name.lower()
            if candidate_lower in header_lower or header_lower in candidate_lower:
                material_candidate = str(value).strip() if value == value else ""
                if material_candidate and material_candidate not in ("-", "N/A", "n/a", "None", "", "####", "#####"):
                    material = clean_material_code(material_candidate)
                    if material:
                        if debug:
                            print(f"[DEBUG] Found material code '{material}' in column '{header}'")
                        break
        if material:
            break

    if not material:
        has_priority = any(
            any(candidate.lower() in header.lower() for header in other_cols.keys())
            for candidate in id_candidates_priority
        )
        if not has_priority:
            for col_name in id_candidates_lower_priority:
                for header, value in other_cols.items():
                    header_lower = header.lower()
                    candidate_lower = col_name.lower()
                    if candidate_lower in header_lower or header_lower in candidate_lower:
                        material_candidate = str(value).strip() if value == value else ""
                        if material_candidate and material_candidate not in ("-", "N/A", "n/a", "None", "", "####", "#####"):
                            material = clean_material_code(material_candidate)
                            if material:
                                if debug:
                                    print(f"[DEBUG] Found material code '{material}' in column '{header}' (using Loc as fallback)")
                                break
                if material:
                    break

    description = ""
    if not material:
        for col_name in id_candidates_fallback:
            for header, value in other_cols.items():
                header_lower = header.lower()
                candidate_lower = col_name.lower()
                if candidate_lower in header_lower or header_lower in candidate_lower:
                    description = str(value).strip() if value == value else ""
                    if description:
                        material = description
                        break
            if material:
                break

    if not material:
        return []

    material = clean_material_code(material)
    if not material:
        return []

    unit = ""
    for header, value in other_cols.items():
        if "unit" in header.lower() and "measure" in header.lower():
            unit = str(value).strip() if value == value else ""
            break
    if not unit:
        unit = "PCE"

    description = ""
    description_candidates = [
        "description",
        "product name",
        "item name",
        "item desc",
    ]
    for header, value in other_cols.items():
        header_lower = header.lower()
        if any(candidate in header_lower for candidate in description_candidates):
            description = str(value).strip() if value == value else ""
            if description:
                break

    notes_candidates = [
        "Notes",
        "Remarks",
        "Comment",
        "Comments",
        "Supplier comments",
        "Bayer comments",
    ]
    notes = ""
    for header, value in other_cols.items():
        header_lower = header.lower()
        if any(candidate.lower() in header_lower for candidate in notes_candidates):
            new_note = str(value).strip() if value == value else ""
            if new_note:
                notes = f"{notes} | {new_note}" if notes else new_note

    final_customer = customer or sender or ""
    requirements: List[Dict[str, Any]] = []

    delivery_date_value = None
    quantity_value = None
    for header, value in other_cols.items():
        header_lower = header.lower()
        if "delivery date" in header_lower:
            delivery_date_value = value
        elif "receipt quantity" in header_lower or header_lower.endswith(" qty") or "quantity" in header_lower:
            quantity_value = value

    if delivery_date_value is not None or quantity_value is not None:
        qty = 0.0
        if quantity_value not in (None, "####"):
            qty = coerce_number(quantity_value)
        delivery_date_iso = _normalize_date_to_iso(str(delivery_date_value or ""))
        requirements.append(
            {
                "customer": final_customer,
                "material": material,
                "quantity": qty,
                "unit": unit,
                "delivery_date": delivery_date_iso,
                "urgency": "",
                "description": description,
                "notes": notes,
                "source": source,
                "source_file": source_file,
                "row_index": row_idx,
            }
        )
        if debug:
            print(
                f"[DEBUG] Row {row_idx}: Extracted requirement "
                f"material={material}, quantity={qty}, delivery_date={delivery_date_iso}"
            )

    if debug and row_idx == 0:
        print(
            f"[DEBUG] Month columns detected ({len(month_cols)}): "
            f"{[col for col, _ in month_cols][:12]}"
        )
    for month_col, qty_val in month_cols:
        qty = 0.0
        if qty_val is not None and qty_val != "####":
            qty = coerce_number(qty_val)
        delivery_date = _normalize_date_to_iso(month_col)
        requirements.append(
            {
                "customer": final_customer,
                "material": material,
                "quantity": qty,
                "unit": unit,
                "delivery_date": delivery_date,
                "urgency": "",
                "description": description,
                "notes": notes,
                "source": source,
                "source_file": source_file,
                "row_index": row_idx,
            }
        )
        if debug:
            print(
                f"[DEBUG] Row {row_idx}: Extracted requirement from month column - "
                f"material={material}, quantity={qty}, month={month_col}, delivery_date={delivery_date}"
            )
    return requirements


def extract_excel_attachments(
    parsed_email,
    bedrock,
    email_customer: str,
    label: str,
    debug: bool = False,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for filename, data in parsed_email.xlsx_attachments or []:
        try:
            records = read_xlsx_bytes(data)
        except Exception:
            records = []
        if not records:
            continue
        attachment_label = filename or f"{label}"
        if debug:
            print(f"[DEBUG] Processing {len(records)} Excel rows from {attachment_label}")
        last_customer = email_customer or parsed_email.sender or ""
        for idx, record in enumerate(records):
            if debug and idx == 0:
                print(f"[DEBUG] Record columns: {list(record.keys())[:12]}")
            customer_columns = ["Customer", "Customer Name", "Company"]
            row_customer = ""
            for col_name in customer_columns:
                for header, value in record.items():
                    if col_name.lower() in str(header).lower():
                        row_customer = str(value).strip() if value == value else ""
                        if row_customer:
                            last_customer = row_customer
                            break
                if row_customer:
                    break
            current_customer = row_customer or last_customer or email_customer or parsed_email.sender or ""
            raw_requirements = extract_requirements_from_excel_row(
                record,
                bedrock,
                parsed_email.sender,
                current_customer,
                "email-xlsx",
                attachment_label,
                idx,
                debug=debug,
            )
            rows.extend(
                sanitize_rows(
                    raw_requirements,
                    current_customer,
                    "email-xlsx",
                    attachment_label,
                )
            )
    return rows

