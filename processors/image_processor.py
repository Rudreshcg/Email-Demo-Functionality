from typing import Dict, List, Tuple

from analysis import (
    analyze_image_requirements,
    analyze_image_table_grid,
    expand_grid_to_requirements,
    resolve_image_month_headers,
)

from .common import sanitize_rows


def _resolve_placeholders(grid: Dict[str, List], bedrock, image_bytes, image_format, debug: bool) -> Dict[str, List]:
    columns = grid.get("columns") or []
    placeholders = [c for c in columns if isinstance(c, str) and c.strip().lower().startswith("m")]
    if not placeholders:
        return grid
    mapping = resolve_image_month_headers(
        bedrock,
        image_bytes=image_bytes,
        image_format=image_format,
        placeholders=placeholders,
        debug=debug,
    )
    if not mapping:
        return grid
    new_columns: List[str] = []
    for col in columns:
        key = str(col)
        new_columns.append(mapping.get(key, col))
    new_rows: List[Dict[str, str]] = []
    for row in grid.get("rows", []):
        if not isinstance(row, dict):
            new_rows.append(row)
            continue
        updated = {}
        for key, value in row.items():
            mapped = mapping.get(str(key), key)
            updated[mapped] = value
        new_rows.append(updated)
    return {"columns": new_columns, "rows": new_rows}


def extract_image_requirements(
    parsed_email,
    bedrock,
    email_customer: str,
    label: str,
    debug: bool = False,
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for index, (ext, img_bytes) in enumerate(parsed_email.images or []):
        image_format = ext.lower()
        if image_format == "jpg":
            image_format = "jpeg"
        attachment_label = f"{label}#image-{index + 1}"
        grid = analyze_image_table_grid(
            bedrock,
            image_bytes=img_bytes,
            image_format=image_format,
            debug=debug,
            context_text=f"From: {parsed_email.sender}\nSubject: {parsed_email.subject}\nDate: {parsed_email.date}",
        )
        attachment_rows: List[Dict[str, str]] = []
        if grid:
            grid = _resolve_placeholders(grid, bedrock, img_bytes, image_format, debug)
            attachment_rows = expand_grid_to_requirements(
                grid,
                "email-image",
                attachment_label,
                email_customer,
                debug=debug,
            )
        if not attachment_rows:
            attachment_rows = analyze_image_requirements(
                bedrock,
                image_bytes=img_bytes,
                image_format=image_format,
                source="email-image",
                source_file=attachment_label,
                debug=debug,
                context_text=f"From: {parsed_email.sender}\nSubject: {parsed_email.subject}\nDate: {parsed_email.date}",
            )
        rows.extend(
            sanitize_rows(
                attachment_rows,
                email_customer,
                "email-image",
                attachment_label,
            )
        )
    return rows

