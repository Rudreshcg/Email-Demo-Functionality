from typing import Any, Dict, List

from analysis import expand_grid_to_requirements
from parsers.html_table_parser import extract_tables_from_html

from .common import sanitize_rows


def extract_html_tables(
    parsed_email,
    email_customer: str,
    label: str,
    debug: bool = False,
) -> List[Dict[str, Any]]:
    if not parsed_email.html_raw:
        return []
    tables = extract_tables_from_html(parsed_email.html_raw)
    if debug and tables:
        print(f"[DEBUG] Found {len(tables)} HTML table(s) in email body")
    all_rows: List[Dict[str, Any]] = []
    for idx, table in enumerate(tables or []):
        table_source = f"{label}#html-table-{idx + 1}"
        if debug:
            print(
                f"[DEBUG] Processing HTML table {idx + 1}: "
                f"columns={len(table.get('columns', []))}, rows={len(table.get('rows', []))}"
            )
        raw_rows = expand_grid_to_requirements(
            table,
            "email-html",
            table_source,
            email_customer,
            debug=debug,
        )
        if not raw_rows:
            continue
        all_rows.extend(
            sanitize_rows(raw_rows, email_customer, "email-html", table_source)
        )
    return all_rows

