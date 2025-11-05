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
	# Try to find code fences anywhere in the text (not just at start)
	# First try to find complete code fences
	m = re.search(r"```[a-zA-Z]*\n?([\s\S]*?)```", text, re.DOTALL)
	if m:
		return m.group(1).strip()
	# If no closing fence, try to extract JSON from opening fence to end of text
	# This handles cases where the response is truncated
	m = re.search(r"```[a-zA-Z]*\n?([\s\S]*?)$", text, re.DOTALL)
	if m:
		extracted = m.group(1).strip()
		# Only return if it looks like JSON (starts with { or [)
		if extracted.startswith(('{', '[')):
			return extracted
	# If no code fences, return original text
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
	except json.JSONDecodeError as e:
		# If JSON is incomplete, try to extract a complete subset
		# Look for the main object and try to complete it
		if try_text.strip().startswith('{'):
			# Strategy: Find the last complete row in the "rows" array
			# Look for pattern: }, followed by whitespace and possible closing brackets
			# Find all complete row objects (ending with })
			import re as _re_json
			
			# Try to find the "rows" array and get all complete row objects
			rows_match = _re_json.search(r'"rows"\s*:\s*\[', try_text)
			if rows_match:
				rows_start = rows_match.end()
				# Find all complete row objects (ending with })
				# Track all complete rows we find
				complete_rows = []
				brace_count = 0
				in_string = False
				escape_next = False
				row_start_pos = -1
				last_complete_row_end = -1
				
				for i in range(rows_start, len(try_text)):
					char = try_text[i]
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
						if char == '{':
							if brace_count == 0:
								row_start_pos = i
							brace_count += 1
						elif char == '}':
							brace_count -= 1
							if brace_count == 0:
								# Found a complete row
								last_complete_row_end = i + 1
								complete_rows.append((row_start_pos, last_complete_row_end))
								# Check if next non-whitespace char is ] or end of string (truncation)
								j = i + 1
								while j < len(try_text) and try_text[j] in ' \t\n\r':
									j += 1
								if j >= len(try_text) or try_text[j] == ']':
									# This is the last complete row (truncation or end of array)
									break
				
				if complete_rows:
					# Reconstruct complete JSON using all complete rows
					# Get the header (everything before rows array)
					header_end = rows_match.end() - 1  # Position of the [
					header = try_text[:header_end]
					
					# Extract all complete row strings
					row_strings = []
					for row_start, row_end in complete_rows:
						row_str = try_text[row_start:row_end]
						row_strings.append(row_str)
					
					# Reconstruct complete JSON
					complete_json = header + '[\n    ' + ',\n    '.join(row_strings) + '\n  ]\n}'
					try:
						data = json.loads(complete_json)
						if isinstance(data, dict) and "columns" in data and "rows" in data:
							return [data]
					except Exception as e2:
						# If that fails, try simpler approach - find last complete object
						pass
			
			# Fallback: Try to find the last complete object/array and close it
			brace_count = 0
			last_valid_pos = -1
			in_string = False
			escape_next = False
			for i, char in enumerate(try_text):
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
					if char == '{':
						brace_count += 1
					elif char == '}':
						brace_count -= 1
						if brace_count == 0:
							last_valid_pos = i + 1
							break
			if last_valid_pos > 0:
				try:
					# Try to parse the truncated but closed JSON
					truncated = try_text[:last_valid_pos]
					data = json.loads(truncated)
					if isinstance(data, dict):
						return [data]
					if isinstance(data, list):
						return data
				except:
					pass
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


def _normalize_date_to_iso(date_str: str) -> str:
    """Normalize any date format to ISO format (YYYY-MM-DD).
    Handles:
    - 2025-03-01 (already ISO)
    - 2025-11-01 00:00:00 (ISO with timestamp)
    - 01-Jan-26 (day-month-year)
    - 01-Mar-25 (day-month-year)
    - Mar-25 (month-year)
    - May-23 (month-year)
    Returns ISO format (YYYY-MM-DD) or original string if parsing fails.
    """
    if not date_str:
        return ""
    
    date_str = str(date_str).strip()
    if not date_str:
        return ""
    
    import re as _re
    
    # Handle ISO format with timestamp: "2025-11-01 00:00:00" -> "2025-11-01"
    iso_with_time_match = _re.match(r"^(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}", date_str)
    if iso_with_time_match:
        return iso_with_time_match.group(1)
    
    # Handle pure ISO format: "2025-03-01" -> return as-is
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    
    # Handle day-month-year format: "01-Jan-26" or "01-Mar-25"
    day_month_year_match = _re.match(r"^(\d{1,2})[-/](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-'](\d{2,4})$", date_str, _re.IGNORECASE)
    if day_month_year_match:
        try:
            day = int(day_month_year_match.group(1))
            mon_str = day_month_year_match.group(2).title()
            year_str = day_month_year_match.group(3)
            year = int(year_str)
            year = 2000 + year if year < 100 else year
            
            MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
            mm = MONTHS.get(mon_str, 1)
            return f"{year:04d}-{mm:02d}-{day:02d}"
        except Exception:
            pass
    
    # Handle month-year format: "Mar-25" or "May-23" -> "2025-03-01" or "2023-05-01"
    return _month_label_to_iso(date_str)


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
        "Extract the COMPLETE visible table into a normalized JSON grid. "
        "Return ONLY one JSON object with keys: columns (array of strings), rows (array of objects). "
        "The first identifier column is often named 'Product Number', 'SKU', or 'Material'. "
        "Keep all column headers exactly as they appear in the image. "
        "\n"
        "CRITICAL EXTRACTION RULES:\n"
        "1. You MUST extract EVERY SINGLE data row from the table - no exceptions, no omissions, no truncation.\n"
        "2. Count the rows in the image first, then ensure your JSON has the EXACT same number of data rows.\n"
        "3. Include ALL rows that contain a valid product number/material ID/SKU (even if some fields are empty).\n"
        "4. Do NOT skip rows, do NOT truncate the response, do NOT stop early.\n"
        "5. If you see a product number (like 80435665, 80116209), you MUST include that entire row.\n"
        "\n"
        "EXCLUSION RULES:\n"
        "- Do NOT include header rows (rows that are clearly column headers)\n"
        "- Do NOT include total/summary rows (rows with 'Total', 'Sum', etc.)\n"
        "- Do NOT include rows without any product number/material ID/SKU\n"
        "\n"
        "SPECIAL INSTRUCTIONS:\n"
        "- If the table has 'Delivery Date' and 'Receipt Quantity' columns, extract each row with its date and quantity.\n"
        "- If the image shows a Rolling Projection band (yellow) with Jul-23 and Aug-23, strictly align numbers under the exact month header.\n"
        "- Include 'Remarks', 'Bayer comments', 'Supplier comments', and all other columns exactly as shown.\n"
        "- Preserve all data values exactly as they appear - do not modify or infer values.\n"
        "\n"
        "VALIDATION:\n"
        "- Before finishing, verify: row count in JSON = data row count in image (excluding headers/totals).\n"
        "- Ensure the JSON is complete and valid - the 'rows' array must contain ALL data rows.\n"
        "- The response must be complete - if you reach max_tokens, you must still include all rows (split if needed, but ensure completeness)."
    )
    if context_text:
        prompt += "\nContext:\n" + context_text[:2000]

    # Use higher max_tokens for large tables - ensure we can capture all rows
    raw = converse_image(bedrock, image_bytes, image_format, prompt, max_tokens=8192)
    if debug:
        print("[DEBUG] Image model raw grid:")
        print(raw[:8000])  # Print more to see if JSON is complete
        print(f"[DEBUG] Full response length: {len(raw)} characters")
    
    # First, try to strip code fences and parse
    data = None
    stripped = _strip_code_fences(raw)
    if stripped != raw:
        if debug:
            print(f"[DEBUG] Stripped code fences, trying to parse (stripped length: {len(stripped)})")
            print(f"[DEBUG] Stripped JSON preview (first 500 chars): {stripped[:500]}")
            print(f"[DEBUG] Stripped JSON preview (last 500 chars): {stripped[-500:]}")
        data = _json_guard(stripped)
        if debug and data:
            print("[DEBUG] Successfully parsed JSON after stripping code fences")
        elif debug:
            print("[DEBUG] Failed to parse stripped JSON, trying other methods")
            # Try to see what the error is
            try:
                import json as _json_test
                _json_test.loads(stripped)
            except Exception as e:
                print(f"[DEBUG] JSON parse error on stripped text: {e}")
    
    # If _json_guard fails (e.g., text before/after JSON), try to extract JSON from text
    # Use stripped version if available, otherwise use raw
    text_to_search = stripped if stripped != raw else raw
    
    if not data:
        import re
        # Try to find a single JSON object {columns: [...], rows: [...]}
        start_pos = text_to_search.find('{')
        if start_pos != -1:
            if debug:
                print(f"[DEBUG] Found opening brace at position {start_pos} in {'stripped' if stripped != raw else 'raw'} text")
            # Find matching closing brace by counting braces
            brace_count = 1
            in_string = False
            escape_next = False
            end_pos = -1
            for i in range(start_pos + 1, len(text_to_search)):
                char = text_to_search[i]
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
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_pos = i + 1
                            break
            if end_pos > start_pos:
                try:
                    json_str = text_to_search[start_pos:end_pos]
                    if debug:
                        print(f"[DEBUG] Extracted JSON object (length {len(json_str)}, start={start_pos}, end={end_pos})")
                        print(f"[DEBUG] JSON preview (first 500 chars): {json_str[:500]}")
                        print(f"[DEBUG] JSON preview (last 500 chars): {json_str[-500:]}")
                    parsed = json.loads(_remove_trailing_commas(_fix_unquoted_thousands_numbers(json_str)))
                    if isinstance(parsed, dict) and "columns" in parsed and "rows" in parsed:
                        data = [parsed]
                        if debug:
                            print(f"[DEBUG] Successfully parsed JSON object with {len(parsed.get('columns', []))} columns and {len(parsed.get('rows', []))} rows")
                except Exception as e:
                    if debug:
                        print(f"[DEBUG] Failed to parse extracted JSON object: {e}")
                        import traceback
                        print(f"[DEBUG] Traceback: {traceback.format_exc()}")
                    data = None
            elif debug:
                print(f"[DEBUG] Could not find matching closing brace (start_pos={start_pos})")
        
        # 1) Try to find JSON array by finding balanced brackets
        # Find the first occurrence of '[{' and then find matching '}]'
        if not data:
            start_pos = text_to_search.find('[{')
            if start_pos != -1:
                # Find matching closing bracket by counting braces and brackets
                bracket_count = 1  # We've seen one '['
                brace_count = 1     # We've seen one '{' inside the '['
                in_string = False
                escape_next = False
                end_pos = -1
                for i in range(start_pos + 2, len(text_to_search)):
                    char = text_to_search[i]
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
                        json_str = text_to_search[start_pos:end_pos]
                        if debug:
                            print(f"[DEBUG] Extracted JSON array substring (length {len(json_str)})")
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
            objs = _parse_loose_array(text_to_search) or []
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
    
    # Validation: Check if grid seems incomplete (very few rows might indicate truncation)
    row_count = len(obj.get('rows', []))
    if row_count < 3 and debug:
        print(f"[WARNING] Grid has only {row_count} rows - this might indicate incomplete extraction. Verify the image has more data rows.")
    
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
    
    NOTE: This function makes an additional API call. It should only be called when
    Jul-23/Aug-23 columns are present to avoid unnecessary API calls and improve performance.
    """
    instruction = (
        "You are given a JSON table grid extracted from an image with month columns. "
        "If both 'Jul-23' and 'Aug-23' columns exist, double-check each row so values are under the correct month. "
        "Do not invent values; only move a value between Jul-23 and Aug-23 if it's clearly placed in the wrong column. "
        "Always return the adjusted grid object in JSON with the same 'columns' and 'rows' structure. "
        "Preserve ALL rows - do not remove or omit any rows."
    )
    prompt = f"{instruction}\n\nGRID:\n{_json_dumps(grid)}"
    # Use lower max_tokens for month correction (smaller, focused task)
    raw = converse_text(bedrock, prompt, system_text=None, max_tokens=4096)
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
    raw = converse_image(bedrock, image_bytes, image_format, vlm_prompt, max_tokens=8192)
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
    header_priority = ["product number", "product id", "sku", "material", "sku code", "product", "code"]
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
    delivery_date_idx = None
    receipt_quantity_idx = None
    unit_of_measure_idx = None
    
    import re as _re
    for i, c in enumerate(columns):
        c_lower = c.lower().strip()
        # Check for month columns (e.g., "Mar-23", "Apr-23")
        if _re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-']?\s?\d{2,4}$", c, _re.IGNORECASE):
            month_indices.append(i)
        # Check for "Delivery Date" column
        elif "delivery date" in c_lower:
            delivery_date_idx = i
        # Check for "Receipt Quantity" column
        elif "receipt quantity" in c_lower:
            receipt_quantity_idx = i
        # Check for "Unit of Measure" column
        elif "unit of measure" in c_lower or "unit" in c_lower and "measure" in c_lower:
            unit_of_measure_idx = i

    # Handle "Delivery Date" and "Receipt Quantity" format (one row per grid row)
    if sku_idx is not None and delivery_date_idx is not None and receipt_quantity_idx is not None:
        if debug:
            print(f"[DEBUG] Found Delivery Date and Receipt Quantity columns - using row-by-row extraction")
            print(f"[DEBUG] SKU column: {columns[sku_idx]}, Delivery Date: {columns[delivery_date_idx]}, Receipt Quantity: {columns[receipt_quantity_idx]}")
        
        out: List[Dict[str, Any]] = []
        for row_idx, row in enumerate(rows):
            if isinstance(row, dict):
                values = [row.get(col, "") for col in columns]
            elif isinstance(row, list):
                values = row + [""] * max(0, len(columns) - len(row))
            else:
                continue
            
            material = str(values[sku_idx]).strip()
            if not material:
                continue
            
            delivery_date_val = str(values[delivery_date_idx]).strip()
            receipt_quantity_val = str(values[receipt_quantity_idx]).strip()
            
            if not delivery_date_val or not receipt_quantity_val:
                continue
            
            # Extract numeric quantity from "Receipt Quantity" (e.g., "1 PCE" -> 1)
            qty_match = _re.search(r"(\d+(?:\.\d+)?)", receipt_quantity_val)
            if qty_match:
                qty = float(qty_match.group(1))
            else:
                try:
                    qty = float(receipt_quantity_val)
                except:
                    continue
            
            if qty <= 0:
                continue
            
            # Extract unit
            unit = ""
            if unit_of_measure_idx is not None:
                unit = str(values[unit_of_measure_idx]).strip()
            if not unit:
                # Try to extract unit from "Receipt Quantity" (e.g., "1 PCE" -> "PCE")
                unit_match = _re.search(r"\d+\s*([A-Za-z]+)", receipt_quantity_val)
                if unit_match:
                    unit = unit_match.group(1).strip()
            
            # Normalize date to ISO format (YYYY-MM-DD)
            delivery_date_iso = _normalize_date_to_iso(delivery_date_val)
            
            # Extract description if available
            description = ""
            desc_candidates = ["Product Short Description", "Description", "Product Description"]
            for desc_col in desc_candidates:
                for i, col in enumerate(columns):
                    if desc_col.lower() in col.lower():
                        description = str(values[i]).strip()
                        break
                if description:
                    break
            
            if debug:
                print(f"[DEBUG] Row {row_idx}: material={material}, quantity={qty}, unit={unit}, delivery_date={delivery_date_iso}")
            
            out.append({
                "customer": customer,
                "material": material,
                "quantity": qty,
                "unit": unit,
                "delivery_date": delivery_date_iso,
                "urgency": "",
                "description": description,
                "notes": description,
                "source": source,
                "source_file": source_file,
                "row_index": row_idx,
            })
        
        if debug:
            print(f"[DEBUG] Extracted {len(out)} requirements from grid (Delivery Date/Receipt Quantity format)")
        return out

    if sku_idx is None or not month_indices:
        if debug:
            print(f"[DEBUG] SKU index: {sku_idx}, Month indices: {month_indices}")
            print(f"[DEBUG] Delivery Date index: {delivery_date_idx}, Receipt Quantity index: {receipt_quantity_idx}")
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
                "delivery_date": _normalize_date_to_iso(month_label),
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
		"\n"
		"CRITICAL EXTRACTION RULES:\n"
		"1. You MUST extract EVERY SINGLE data row from the table - no exceptions, no omissions.\n"
		"2. Count the data rows in the image first, then ensure your JSON has the EXACT same number of entries.\n"
		"3. Include ALL rows that contain a valid product number/material ID/SKU (even if quantity is 0 or blank).\n"
		"4. Do NOT skip rows, do NOT truncate, do NOT stop early.\n"
		"5. If you see a product number (like 80435665, 80116209), you MUST include that entire row with all its data.\n"
		"\n"
		"QUANTITY COLUMN IDENTIFICATION:\n"
		"- CRITICAL: Identify the QUANTITY column correctly. Look for columns named 'Receipt Quantity', 'Quantity', 'Order Quantity', 'Qty', or similar.\n"
		"- DO NOT confuse 'Order Leadtime' (which is a time duration like '180 days', '90 days') with quantity.\n"
		"- Quantity should be a number with units (e.g., '1 PCE', '2 PCE', '10 PCE', '100', '50 KG').\n"
		"- Order Leadtime is NOT quantity - it's the lead time in days.\n"
		"- Quantity is the actual order amount in the 'Receipt Quantity' column.\n"
		"\n"
		"EXTRACTION FORMATS:\n"
		"- If the table has 'Delivery Date' and 'Receipt Quantity' columns: extract ONE JSON row per data row in the table.\n"
		"- Map: Product Number/Product ID/SKU -> material (numeric part only), Delivery Date -> delivery_date, Receipt Quantity -> quantity.\n"
		"- For example: if Receipt Quantity is '1 PCE', extract quantity=1 and unit='PCE'.\n"
		"- If the table has month columns (e.g., Mar-23, Apr-23): emit ONE JSON row per non-zero month value for each SKU.\n"
		"\n"
		"EXCLUSION RULES:\n"
		"- Do NOT extract total rows, summary rows, header rows, or rows without material/product ID/SKU.\n"
		"- Do NOT extract rows that are just customer names or headings without material/quantity data.\n"
		"- If 'Remarks' contains 'Dropped', skip that SKU.\n"
		"- If a Batch size column exists, do not treat it as quantity.\n"
		"- Do not include rows where quantity is 0 or blank (but still extract the row if it has a valid product number).\n"
		"\n"
		"DATE FORMATTING:\n"
		"- For dates like '01-Mar-25', convert to ISO format '2025-03-01'.\n"
		"- Normalize month like 'May-23' to ISO date '2023-05-01' when possible.\n"
		"- Align quantities STRICTLY under their exact month headers; do NOT shift values.\n"
		"\n"
		"OUTPUT FORMAT:\n"
		"- Return ONLY JSON (no markdown), as a list of objects with keys:\n"
		"  customer (customer name if found in row, otherwise empty), material (numeric product ID only), quantity (numeric value only - NOT Order Leadtime), unit (unit of measure like 'PCE', 'KG', etc.), delivery_date (ISO format YYYY-MM-DD), description (product/item description if available), urgency, notes, source, source_file, row_index.\n"
		"- CRITICAL: Do NOT extract generic phrases like 'Material Requirements', 'Customer Material Requirements', 'Requirements', 'Material', 'Excel attachment', 'Table', 'Table as image', 'Format', 'Image', 'Attachment', 'Test', 'Welcome', 'Hey' as customer names.\n"
		"- Only extract actual company/customer names that look like real business names.\n"
		"- If you cannot find a valid customer/company name, leave the customer field empty.\n"
		"- For unknown fields use empty string.\n"
		"\n"
		"VALIDATION:\n"
		"- Before finishing, verify: number of JSON entries = number of data rows in image (excluding headers/totals).\n"
		"- Ensure the JSON is complete and valid - the array must contain ALL data rows.\n"
		"- The response must be complete - include all rows even if you reach max_tokens."
	)

	if context_text:
		prompt = (
			prompt
			+ "\nContext (may include customer/sender, subject, date):\n"
			+ context_text[:2000]
		)

	# Use higher max_tokens for large tables - ensure we can capture all rows
	raw = converse_image(bedrock, image_bytes, image_format, prompt, max_tokens=8192)
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
