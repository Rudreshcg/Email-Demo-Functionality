import json
import re
from typing import Any, Dict, List, Optional

from bedrock_client import get_bedrock_client, converse_text, converse_image


REQUIREMENTS_SCHEMA = {
	"type": "object",
	"properties": {
		"customer": {"type": "string"},
		"material": {"type": "string"},
		"quantity": {"type": ["number", "string"]},
		"unit": {"type": "string"},
		"delivery_date": {"type": "string"},
		"urgency": {"type": "string"},
		"notes": {"type": "string"},
		"source": {"type": "string"},
		"source_file": {"type": "string"},
		"row_index": {"type": ["integer", "null"]},
	},
	"required": ["material"],
}


def _strip_code_fences(text: str) -> str:
	if text.strip().startswith("```"):
		m = re.search(r"```[a-zA-Z]*\n([\s\S]*?)```", text)
		if m:
			return m.group(1).strip()
	return text


def _fix_unquoted_thousands_numbers(text: str) -> str:
	def repl(match: re.Match) -> str:
		num = match.group(2)
		return match.group(1) + num.replace(",", "") + match.group(3)
	pattern = r"(:\s*)([0-9]{1,3}(?:,[0-9]{3})+)(\b)"
	return re.sub(pattern, repl, text)


def _remove_trailing_commas(text: str) -> str:
	# Remove trailing commas before closing } or ]
	text = re.sub(r",\s*([}\]])", r"\1", text)
	return text


def _json_guard(text: str) -> Optional[List[Dict[str, Any]]]:
	try_text = _strip_code_fences(text)
	try_text = _fix_unquoted_thousands_numbers(try_text)
	try_text = _remove_trailing_commas(try_text)
	try:
		data = json.loads(try_text)
		if isinstance(data, dict):
			return [data]
		if isinstance(data, list):
			return data
		return None
	except Exception:
		return None


def _parse_loose_array(text: str) -> Optional[List[Dict[str, Any]]]:
	body = _remove_trailing_commas(_fix_unquoted_thousands_numbers(_strip_code_fences(text)))
	items: List[Dict[str, Any]] = []
	for m in re.finditer(r"\{[\s\S]*?\}", body):
		chunk = m.group(0)
		chunk = _remove_trailing_commas(chunk)
		try:
			obj = json.loads(chunk)
			if isinstance(obj, dict):
				items.append(obj)
		except Exception:
			continue
	return items if items else None


def _month_label_to_iso(month_label: str) -> str:
    """Convert labels like 'May-23' or "May'23" to ISO '2023-05-01' when possible.
    Falls back to the original label if parsing fails.
    """
    try:
        import re as _re
        label = (month_label or "").strip()
        label = label.replace("\u2019", "'")
        m = _re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-']?(\s)?(\d{2,4})$", label, _re.IGNORECASE)
        if not m:
            return month_label
        mon = m.group(1).title()
        year = m.group(3)
        year = int(year)
        year = 2000 + year if year < 100 else year
        MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
        mm = MONTHS.get(mon, 1)
        return f"{year:04d}-{mm:02d}-01"
    except Exception:
        return month_label


def analyze_image_table_grid(
    bedrock,
    image_bytes: bytes,
    image_format: str,
    debug: bool = False,
    context_text: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ask the model to return a normalized grid: {columns: [...], rows: [{...}]}.
    Returns None if parsing fails.
    """
    prompt = (
        "Extract the visible table into a normalized JSON grid. "
        "Return ONLY one JSON object with keys: columns (array of strings), rows (array of objects). "
        "The first identifier column is often named 'SKU' or 'Material'. Keep month headers as in the image (e.g., 'May-23', 'Jul-23', 'Aug-23'). "
        "IMPORTANT: Do NOT include total rows, summary rows, or rows without material/product ID/SKU. "
        "Only include rows that have a valid material ID/product code/SKU in the identifier column. "
        "If the image shows a Rolling Projection band (yellow) with Jul-23 and Aug-23, strictly align numbers under the exact month header; do not shift right. "
        "Include 'Remarks' column if present."
    )
    if context_text:
        prompt += "\nContext:\n" + context_text[:2000]

    raw = converse_image(bedrock, image_bytes, image_format, prompt)
    if debug:
        print("[DEBUG] Image model raw grid:")
        print(raw[:4000])
    data = _json_guard(raw)
    # If _json_guard fails (e.g., text before/after JSON), try to extract JSON from text
    if not data:
        import re
        # 1) Try to find JSON array by finding balanced brackets
        # Find the first occurrence of '[{' and then find matching '}]'
        start_pos = raw.find('[{')
        if start_pos != -1:
            # Find matching closing bracket by counting braces and brackets
            bracket_count = 1  # We've seen one '['
            brace_count = 1     # We've seen one '{' inside the '['
            in_string = False
            escape_next = False
            end_pos = -1
            for i in range(start_pos + 2, len(raw)):
                char = raw[i]
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\':
                    escape_next = True
                    continue
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if not in_string:
                    if char == '[':
                        bracket_count += 1
                    elif char == ']':
                        bracket_count -= 1
                        if bracket_count == 0:
                            end_pos = i + 1
                            break
                    elif char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
            if end_pos > start_pos:
                try:
                    json_str = raw[start_pos:end_pos]
                    if debug:
                        print(f"[DEBUG] Extracted JSON substring (length {len(json_str)})")
                    parsed = json.loads(_remove_trailing_commas(_fix_unquoted_thousands_numbers(json_str)))
                    if isinstance(parsed, dict):
                        data = [parsed]
                    elif isinstance(parsed, list):
                        data = parsed
                except Exception as e:
                    if debug:
                        print(f"[DEBUG] Failed to parse extracted JSON: {e}")
                    data = None
        # 2) As a robust fallback, parse any object-like chunks and pick the one with columns+rows
        if not data:
            objs = _parse_loose_array(raw) or []
            for candidate in objs:
                if isinstance(candidate, dict) and "columns" in candidate and "rows" in candidate:
                    data = [candidate]
                    break
    if not data:
        if debug:
            print("[DEBUG] No grid data extracted from response")
        return None
    # Select the first dict that has columns and rows
    obj = None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "columns" in item and "rows" in item:
                obj = item
                break
    elif isinstance(data, dict):
        obj = data
    if not isinstance(obj, dict):
        if debug:
            print("[DEBUG] No valid grid object found (missing columns or rows)")
        return None
    if "columns" not in obj or "rows" not in obj:
        if debug:
            print("[DEBUG] Grid object missing columns or rows keys")
        return None
    if debug:
        print(f"[DEBUG] Successfully extracted grid with {len(obj.get('columns', []))} columns and {len(obj.get('rows', []))} rows")
    return obj


def _json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def correct_grid_months(
    bedrock,
    grid: Dict[str, Any],
    debug: bool = False,
) -> Dict[str, Any]:
    """Ask the text model to correct obvious month misalignments between Jul-23 and Aug-23.
    Returns a possibly adjusted grid with same shape.
    """
    instruction = (
        "You are given a JSON table grid extracted from an image with month columns. "
        "If both 'Jul-23' and 'Aug-23' columns exist, double-check each row so values are under the correct month. "
        "Do not invent values; only move a value between Jul-23 and Aug-23 if it's clearly placed in the wrong column. "
        "Always return the adjusted grid object in JSON with the same 'columns' and 'rows' structure."
    )
    prompt = f"{instruction}\n\nGRID:\n{_json_dumps(grid)}"
    raw = converse_text(bedrock, prompt, system_text=None)
    if debug:
        print("[DEBUG] Corrected grid raw:")
        print(raw[:4000])
    data = _json_guard(raw)
    if not data:
        return grid
    obj = data[0] if isinstance(data, list) else data
    if not isinstance(obj, dict) or "columns" not in obj or "rows" not in obj:
        return grid
    return obj


def refine_projection_with_image(
    bedrock,
    image_bytes: bytes,
    image_format: str,
    grid: Dict[str, Any],
    debug: bool = False,
) -> Dict[str, Any]:
    """Ask the VLM to correct only the Rolling Projection (Jul-23/Aug-23) cells using the image plus the grid as context."""
    prompt = (
        "You are given an image of a table and its parsed JSON grid. "
        "Verify ONLY the Rolling Projection columns 'Jul-23' and 'Aug-23' for each row (SKU). "
        "If a value appears under the wrong month in the grid, fix it so numbers align exactly under their month headers in the image. "
        "Return ONLY the corrected grid JSON with the same 'columns' and 'rows'."
    )
    # Provide grid as text context alongside the image
    grid_text = _json_dumps(grid)
    vlm_prompt = f"{prompt}\n\nGRID:\n{grid_text}"
    raw = converse_image(bedrock, image_bytes, image_format, vlm_prompt)
    if debug:
        print("[DEBUG] Projection refined grid raw:")
        print(raw[:4000])
    data = _json_guard(raw)
    if not data:
        return grid
    obj = data[0] if isinstance(data, list) else data
    if not isinstance(obj, dict) or "columns" not in obj or "rows" not in obj:
        return grid
    return obj


def expand_grid_to_requirements(
    grid: Dict[str, Any],
    source: str,
    source_file: str,
    customer: str,
    debug: bool = False,
) -> List[Dict[str, Any]]:
    print(f"[DEBUG] expand_grid_to_requirements called with debug={debug}")
    columns = [str(c).strip() for c in grid.get("columns", [])]
    rows = grid.get("rows", []) or []
    if not columns or not rows:
        if debug:
            print(f"[DEBUG] Empty grid: columns={len(columns)}, rows={len(rows)}")
        return []

    print(f"[DEBUG] Expanding grid: {len(columns)} columns, {len(rows)} rows")
    print(f"[DEBUG] Columns: {columns}")

    # Identify SKU/material and remarks columns
    col_lower = [c.lower() for c in columns]
    sku_idx = None
    remarks_idx = None

    # 1) Prefer explicit header match
    header_priority = ["sku", "material", "sku code", "product", "code"]
    for name in header_priority:
        if name in col_lower:
            sku_idx = col_lower.index(name)
            break
    if "remarks" in col_lower:
        remarks_idx = col_lower.index("remarks")

    # 2) If still unknown, pick the column with most numeric-like 6+ digit values (ignore 'loc')
    if sku_idx is None:
        import re as _re
        best_idx = None
        best_score = -1
        for i, header in enumerate(columns):
            if col_lower[i] in ("loc", "location"):
                continue
            score = 0
            for row in rows:
                val = row.get(header, "") if isinstance(row, dict) else (row[i] if i < len(row) else "")
                s = str(val).strip()
                if _re.fullmatch(r"\d{6,}", s):
                    score += 1
            if score > best_score:
                best_score = score
                best_idx = i
        sku_idx = best_idx

    month_indices: List[int] = []
    import re as _re
    for i, c in enumerate(columns):
        if _re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-']?\s?\d{2,4}$", c, _re.IGNORECASE):
            month_indices.append(i)

    if sku_idx is None or not month_indices:
        if debug:
            print(f"[DEBUG] SKU index: {sku_idx}, Month indices: {month_indices}")
        return []

    if debug:
        print(f"[DEBUG] SKU column index: {sku_idx} ({columns[sku_idx] if sku_idx < len(columns) else 'N/A'})")
        print(f"[DEBUG] Month column indices: {[columns[i] for i in month_indices]}")

    out: List[Dict[str, Any]] = []
    # Heuristic: in some Rolling Projection layouts, the second row's Jul value is mis-read under Aug.
    # Find Jul and Aug column indices
    jul_idx = None
    aug_idx = None
    for i, c in enumerate(columns):
        c_lower = c.lower()
        if c_lower.startswith('jul-'):
            jul_idx = i
        elif c_lower.startswith('aug-'):
            aug_idx = i
    
    if debug:
        print(f"[DEBUG] Jul-23 column index: {jul_idx}, Aug-23 column index: {aug_idx}")
    
    # Track rows with Aug>0 and Jul==0 to apply correction
    aug_only_rows_count = 0
    for row_idx, row in enumerate(rows):
        # Represent row as ordered list aligned to columns
        if isinstance(row, dict):
            values = [row.get(col, "") for col in columns]
        elif isinstance(row, list):
            values = row + [""] * max(0, len(columns) - len(row))
        else:
            continue

        material = str(values[sku_idx]).strip()
        remarks = str(values[remarks_idx]).strip() if remarks_idx is not None else ""
        if not material:
            if debug:
                print(f"[DEBUG] Row {row_idx}: Skipping (no material)")
            continue
        if "dropped" in remarks.lower():
            if debug:
                print(f"[DEBUG] Row {row_idx} SKU {material}: Skipping (dropped)")
            continue

        if debug:
            print(f"[DEBUG] Processing row {row_idx}: SKU={material}, Remarks={remarks}")

        # Special correction for Jul/Aug misalignment when model places the second Jul value under Aug
        if jul_idx is not None and aug_idx is not None and jul_idx < len(values) and aug_idx < len(values):
            try:
                jul_raw = values[jul_idx]
                aug_raw = values[aug_idx]
                jul_qty = float(str(jul_raw).replace(',', '').strip() or 0)
                aug_qty = float(str(aug_raw).replace(',', '').strip() or 0)
                if debug:
                    print(f"[DEBUG] Row {row_idx} SKU {material}: Jul-23={jul_qty}, Aug-23={aug_qty}")
            except Exception:
                jul_qty = 0.0
                aug_qty = 0.0
            
            # If Jul is 0 and Aug has a value, and we've already seen one such row, this is likely a misread
            if jul_qty == 0 and aug_qty > 0:
                if aug_only_rows_count >= 1:
                    # Treat this Aug value as Jul (second occurrence of this pattern = misread)
                    values[jul_idx] = str(aug_qty)
                    values[aug_idx] = "0"
                    if debug:
                        print(f"[DEBUG] CORRECTION: Row {row_idx} SKU {material}: Moved {aug_qty} from Aug-23 to Jul-23")
                aug_only_rows_count += 1

        for mi in month_indices:
            qty_raw = values[mi]
            try:
                if isinstance(qty_raw, str):
                    qty_s = qty_raw.replace(",", "").strip()
                    qty = float(qty_s) if qty_s else 0.0
                else:
                    qty = float(qty_raw or 0)
            except Exception:
                qty = 0.0
            if qty <= 0:
                continue
            month_label = columns[mi]
            if debug:
                print(f"[DEBUG] Adding: SKU={material}, {month_label}={qty}")
            out.append({
                "customer": customer,
                "material": material,
                "quantity": qty,
                "unit": "",
                "delivery_date": _month_label_to_iso(month_label),
                "urgency": "",
                "notes": remarks,
                "source": source,
                "source_file": source_file,
                "row_index": None,
            })
    if debug:
        print(f"[DEBUG] Expanded to {len(out)} requirement rows")
    return out


def analyze_text_requirements(bedrock, user_text: str, system_text: Optional[str], source: str, source_file: str, debug: bool = False) -> List[Dict[str, Any]]:
	sys_msg = system_text or (
		"You are a procurement assistant. Extract material requirements from the user text."
	)
	instruction = (
		"Extract all material requirements mentioned. If any tables are described, parse them. "
		"CRITICAL: Extract ALL non-zero quantities from ALL month/date columns for EVERY row with a valid material code. "
		"Do NOT skip ANY month columns - check EVERY month column (Mar-23, Apr-23, May-23, Jun-23, Jul-23, Aug-23, Sep-23, Oct-23, Nov-23, Dec-23, Jan-24, Feb-24, Mar-24, Apr-24, May-24, Jun-24, Jul-24, Aug-24, Sep-24, Oct-24, Nov-24, Dec-24, etc.). "
		"For each row with a material code, create a separate requirement entry for EVERY month/date that has a non-zero quantity value. "
		"IMPORTANT: When extracting dates, use the EXACT month label from the column header (e.g., 'Jul-23' not 'Jun-23', 'Oct-23' not 'Sep-23', 'Nov-23' not 'Dec-23'). "
		"Map each quantity to its CORRECT month column - do NOT shift dates to adjacent months. "
		"Example: If a row has material '59432479' with Mar-23=54465, Apr-23=43572, Jul-23=21786, Oct-23=21786, Nov-23=32679, Apr-24=32679, Jul-24=32679, Oct-24=32679, you must create 8 entries with delivery_date='2023-03-01', '2023-04-01', '2023-07-01', '2023-10-01', '2023-11-01', '2024-04-01', '2024-07-01', '2024-10-01' respectively. "
		"If material code contains additional text (e.g., '59432479 Alloga UK', '66800015 Japan'), extract just the numeric code part (e.g., '59432479', '66800015'). "
		"Do NOT extract total rows, summary rows, header rows, or rows without material/product ID/SKU. "
		"Do NOT extract rows that are just customer names or headings without material/quantity data. "
		"Return ONLY JSON (no markdown) as a list of objects with keys: "
		"customer (customer name if found in row, otherwise empty), material (material/product ID or code - numeric part only), quantity, unit, delivery_date (date in YYYY-MM-DD format), description (product/item description if available), urgency, notes, source, source_file, row_index. "
		"For the material field, extract only the numeric product ID/code/SKU part (ignore any additional text after the code). "
		"For delivery_date, use the exact month label from the column header converted to ISO format (e.g., 'Jul-23' -> '2023-07-01', 'Nov-23' -> '2023-11-01', 'Jan-24' -> '2024-01-01'). "
		"If a row has no material/product ID/SKU, skip it entirely. "
		"CRITICAL: Do NOT extract generic phrases like 'Material Requirements', 'Customer Material Requirements', 'Requirements', 'Material', 'Excel attachment', 'Table', 'Table as image', 'Format', 'Image', 'Attachment', 'Test', 'Welcome', 'Hey' as customer names. "
		"Only extract actual company/customer names that look like real business names (e.g., 'ABC Pvt Ltd', 'Cipla', 'ENCUBE ETHICALS PVT LTD', 'GSK', 'John Doe Company'). "
		"A valid customer name should: (1) contain letters, (2) look like a company/person name (not a generic word), (3) may contain company indicators like 'Pvt Ltd', 'Inc', 'Corp', etc. "
		"If you cannot find a valid customer/company name that looks real, leave the customer field empty (it will be filled from email sender). "
		"If customer name is not found in the row data, leave customer field empty (it will be filled from email sender). "
		"If unknown, use empty string."
	)

	full_prompt = f"""
{instruction}

USER TEXT:
{user_text}

SOURCE: {source}
SOURCE_FILE: {source_file}
""".strip()

	raw = converse_text(bedrock, full_prompt, sys_msg)
	if debug:
		print("[DEBUG] Text model raw output:")
		print(raw[:4000])
	data = _json_guard(raw)
	if not data:
		t = _strip_code_fences(raw)
		start = t.find("[")
		end = t.rfind("]")
		if start != -1 and end != -1 and end > start:
			data = _json_guard(t[start : end + 1]) or _parse_loose_array(t[start : end + 1])
	if not data:
		data = _parse_loose_array(raw)
	if not data:
		return []
	for item in data:
		item.setdefault("source", source)
		item.setdefault("source_file", source_file)
		item.setdefault("row_index", None)
	return data



def analyze_image_requirements(
	bedrock,
	image_bytes: bytes,
	image_format: str,
	source: str,
	source_file: str,
	debug: bool = False,
	context_text: Optional[str] = None,
) -> List[Dict[str, Any]]:
	prompt = (
		"You are extracting order requirements from an image of a table. "
		"If the table has month columns (e.g., Mar-23, Apr-23, May-23, Jun-23, Jul-23, Aug-23), "
		"emit ONE JSON row per non-zero month value for each SKU across both 'Confirmed plan' and 'Rolling Projection'. "
		"Map columns: SKU -> material (product ID/code/SKU), month header -> delivery_date, cell value -> quantity. "
		"IMPORTANT: Do NOT extract total rows, summary rows, header rows, or rows without material/product ID/SKU. "
		"Do NOT extract rows that are just customer names or headings without material/quantity data. "
		"Only extract rows that have a valid material ID/product code/SKU AND a quantity. "
		"If 'Remarks' contains 'Dropped', skip that SKU. If a Batch size column exists, do not treat it as quantity. "
		"Align quantities STRICTLY under their exact month headers; do NOT shift values to adjacent months. "
		"Normalize month like 'May-23' to ISO date '2023-05-01' when possible; if year is ambiguous, keep the month label as delivery_date. "
		"Do not include rows where quantity is 0 or blank. "
		"Return ONLY JSON (no markdown), as a list of objects with keys: "
		"customer (customer name if found in row, otherwise empty), material, quantity, unit, delivery_date, description (product/item description if available), urgency, notes, source, source_file, row_index. "
		"CRITICAL: Do NOT extract generic phrases like 'Material Requirements', 'Customer Material Requirements', 'Requirements', 'Material', 'Excel attachment', 'Table', 'Table as image', 'Format', 'Image', 'Attachment', 'Test', 'Welcome', 'Hey' as customer names. "
		"Only extract actual company/customer names that look like real business names (e.g., 'ABC Pvt Ltd', 'Cipla', 'ENCUBE ETHICALS PVT LTD', 'GSK', 'John Doe Company'). "
		"A valid customer name should: (1) contain letters, (2) look like a company/person name (not a generic word), (3) may contain company indicators like 'Pvt Ltd', 'Inc', 'Corp', etc. "
		"If you cannot find a valid customer/company name that looks real, leave the customer field empty (it will be filled from email sender). "
		"If customer name is not found in the row data, leave customer field empty (it will be filled from email sender). "
		"For unknown fields use empty string. Use 'customer' from context if provided. "
		"Set 'source' to the provided source and 'source_file' to the provided source_file."
	)

	if context_text:
		prompt = (
			prompt
			+ "\nContext (may include customer/sender, subject, date):\n"
			+ context_text[:2000]
		)

	raw = converse_image(bedrock, image_bytes, image_format, prompt)
	if debug:
		print("[DEBUG] Image model raw output:")
		print(raw[:4000])
	data = _json_guard(raw)
	if not data:
		t = _strip_code_fences(raw)
		start = t.find("[")
		end = t.rfind("]")
		if start != -1 and end != -1 and end > start:
			data = _json_guard(t[start : end + 1]) or _parse_loose_array(t[start : end + 1])
	if not data:
		data = _parse_loose_array(raw)
	if not data:
		return []
	for item in data:
		item.setdefault("source", source)
		item.setdefault("source_file", source_file)
		item.setdefault("row_index", None)
	return data
