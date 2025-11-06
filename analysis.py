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
    """Convert labels like 'May-23' or "May'23" or "June'23" or "July'23" or "Sept'23" to ISO '2023-05-01' when possible.
    Falls back to the original label if parsing fails.
    Handles both abbreviated (Jun, Jul, Sep) and full month names (June, July, Sept).
    """
    try:
        import re as _re
        label = (month_label or "").strip()
        label = label.replace("\u2019", "'")
        # Handle both abbreviated and full month names (e.g., "June", "July", "Sept")
        # Extract month name and year separately
        m = _re.match(r"^(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[-']?\s?(\d{2,4})$", label, _re.IGNORECASE)
        if not m:
            return month_label
        mon = m.group(1).title()
        # Find the year - it's the last group that matches digits
        year_str = None
        for i in range(len(m.groups()), 0, -1):
            g = m.group(i)
            if g and _re.match(r'^\d{2,4}$', g):
                year_str = g
                break
        if not year_str:
            return month_label
        year = int(year_str)
        year = 2000 + year if year < 100 else year
        # Map both abbreviated and full month names to numbers
        MONTHS = {
            "Jan":1, "January":1,
            "Feb":2, "February":2,
            "Mar":3, "March":3,
            "Apr":4, "April":4,
            "May":5,
            "Jun":6, "June":6,
            "Jul":7, "July":7,
            "Aug":8, "August":8,
            "Sep":9, "Sept":9, "September":9,
            "Oct":10, "October":10,
            "Nov":11, "November":11,
            "Dec":12, "December":12
        }
        # Normalize month name - handle variations
        if mon.startswith("Jun") and len(mon) > 3:
            mon = "June"
        elif mon.startswith("Jul") and len(mon) > 3:
            mon = "July"
        elif mon.startswith("Sep") and len(mon) > 3:
            mon = "Sept"  # Prefer "Sept" over "September" for matching
        elif mon.startswith("Jan") and len(mon) > 3:
            mon = "January"
        elif mon.startswith("Feb") and len(mon) > 3:
            mon = "February"
        elif mon.startswith("Mar") and len(mon) > 3:
            mon = "March"
        elif mon.startswith("Apr") and len(mon) > 3:
            mon = "April"
        elif mon.startswith("Aug") and len(mon) > 3:
            mon = "August"
        elif mon.startswith("Oct") and len(mon) > 3:
            mon = "October"
        elif mon.startswith("Nov") and len(mon) > 3:
            mon = "November"
        elif mon.startswith("Dec") and len(mon) > 3:
            mon = "December"
        
        mm = MONTHS.get(mon, MONTHS.get(mon[:3], 1))  # Try full name, then first 3 chars
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
        "\n"
        "THINK LIKE A HUMAN - UNDERSTAND THE TABLE:\n"
        "1. Look at the table structure - understand what each column represents by its header and data patterns.\n"
        "2. Column names may vary - identify columns by their MEANING, not just exact names.\n"
        "3. Understand the table layout - identify identifier columns, description columns, quantity columns, date columns, etc.\n"
        "4. Read the table systematically - extract all data rows completely.\n"
        "\n"
        "INTELLIGENT COLUMN IDENTIFICATION:\n"
        "1. MATERIAL/PRODUCT IDENTIFIER COLUMN:\n"
        "   - Look for columns containing product codes, SKUs, material IDs, or item identifiers.\n"
        "   - Common names: 'Product Number', 'SKU', 'Material', 'Item Code', 'Product ID', 'Code', 'Part Number', 'Loc' (if it's the identifier).\n"
        "   - If multiple identifier columns exist, use the one that appears to be the primary product identifier.\n"
        "   - If both 'Loc' and 'SKU' exist, typically 'SKU' is the product identifier (use that), while 'Loc' might be location.\n"
        "\n"
        "2. DESCRIPTION COLUMN:\n"
        "   - Look for columns with product descriptions, item names, or product information.\n"
        "   - Common names: 'Description', 'Product Description', 'Item Description', 'Product Name', 'Product Short Description'.\n"
        "\n"
        "3. QUANTITY/DATE COLUMNS:\n"
        "   - Month columns: Headers like 'Mar-23', 'Apr-23', 'May-23', 'Jun-23', 'Jul-23', 'Aug-23', etc. - extract from ALL of them.\n"
        "   - Date/Quantity pairs: If you see 'Delivery Date' and 'Receipt Quantity' columns, extract each row with its date and quantity.\n"
        "   - Count ALL month columns from first to last - do NOT miss any, including the last one.\n"
        "\n"
        "4. OTHER COLUMNS:\n"
        "   - Extract all other columns exactly as shown (Remarks, Comments, etc.).\n"
        "   - Preserve column headers exactly as they appear - do not modify names.\n"
        "\n"
        "CRITICAL EXTRACTION RULES:\n"
        "1. You MUST extract EVERY SINGLE data row from the table - no exceptions, no omissions, no truncation.\n"
        "2. Count the rows in the image first, then ensure your JSON has the EXACT same number of data rows.\n"
        "3. Include ALL rows that contain a valid product number/material ID/SKU (even if some fields are empty).\n"
        "4. Do NOT skip rows, do NOT truncate the response, do NOT stop early.\n"
        "5. If you see a product number (like 80435665, 80116209, ABC, DEF), you MUST include that entire row.\n"
        "6. Extract ALL columns for each row - do not omit any columns.\n"
        "\n"
        "EXCLUSION RULES:\n"
        "- Do NOT include header rows (rows that are clearly column headers)\n"
        "- Do NOT include total/summary rows (rows with 'Total', 'Sum', etc.)\n"
        "- Do NOT include rows without any product number/material ID/SKU\n"
        "\n"
        "DATA PRESERVATION:\n"
        "- Keep all column headers exactly as they appear in the image.\n"
        "- Preserve all data values exactly as they appear - do not modify or infer values.\n"
        "- CRITICAL: If a cell is empty, blank, or missing, extract it as null, empty string \"\", or 0 - do NOT infer a value, do NOT use a value from another cell.\n"
        "- CRITICAL: Do NOT shift values between columns - if a cell is empty, it means that cell is empty, NOT the value from another column.\n"
        "- CRITICAL: Do NOT fill empty cells with values from adjacent cells - if a cell is empty, it stays empty (or becomes 0 for quantity columns).\n"
        "- CRITICAL: If you cannot clearly see a value in a cell, extract it as null or empty - do NOT guess or infer.\n"
        "- Extract all columns, even if some seem less important.\n"
        "- Extract all rows completely - do NOT miss any data.\n"
        "\n"
        "VALIDATION:\n"
        "- Before finishing, verify: row count in JSON = data row count in image (excluding headers/totals).\n"
        "- Verify: column count in JSON = column count in image.\n"
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

    # Identify SKU/material and remarks columns - intelligently identify by meaning
    col_lower = [c.lower() for c in columns]
    sku_idx = None
    remarks_idx = None

    # 1) Prefer explicit header match - prioritize SKU/Product Number over Loc
    # Priority order: SKU and Product Number first, then others, Loc last
    header_priority = ["sku", "product number", "product id", "material", "item code", "item number", "product code", "part number", "code", "product"]
    for name in header_priority:
        for i, col in enumerate(col_lower):
            if name in col or col == name:
                sku_idx = i
                if debug:
                    print(f"[DEBUG] Found SKU column '{columns[i]}' (priority match: {name})")
                break
        if sku_idx is not None:
            break
    
    # 2) If no priority column found, check for Loc (but only if no SKU/Product Number exists)
    if sku_idx is None:
        # Check if any priority column exists in the table
        has_priority_column = any(any(pri in col for pri in ["sku", "product number", "product id", "material"]) for col in col_lower)
        if not has_priority_column:
            for i, col in enumerate(col_lower):
                if "loc" in col and col != "location":
                    sku_idx = i
                    if debug:
                        print(f"[DEBUG] Found SKU column '{columns[i]}' (using Loc as fallback)")
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
        # Check for month columns (e.g., "Mar-23", "Apr-23", "May'23", "June'23", "July'23", "Sept'23", "1-May-23", "23-Mar")
        # Handle both abbreviated (Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec)
        # and full month names (January, February, March, April, May, June, July, August, September, October, November, December)
        # Also handle variations like "Sept" for September
        # Also handle formats like "1-May-23", "1-.1", "23-Mar", "23-Apr" (date columns that might be month indicators)
        month_pattern = r"^(\d+[-.])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$"
        if _re.match(month_pattern, c, _re.IGNORECASE):
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
            
            delivery_date_val = str(values[delivery_date_idx]).strip() if delivery_date_idx < len(values) else ""
            receipt_quantity_val = str(values[receipt_quantity_idx]).strip() if receipt_quantity_idx < len(values) else ""
            
            # Handle empty cells - treat empty receipt quantity as 0, but still need a date
            if not delivery_date_val:
                continue  # Skip if no date
            
            # If receipt quantity is empty, treat as 0 but still capture the row
            if not receipt_quantity_val or receipt_quantity_val in ("", "-", "N/A", "n/a", "None", "####"):
                receipt_quantity_val = "0"  # Treat empty as zero
            
            # Extract numeric quantity from "Receipt Quantity" (e.g., "1 PCE" -> 1, "0" -> 0)
            # Handle "####" which is Excel's way of showing a number that's too wide - treat as 0 or try to parse
            qty = 0.0  # Default to 0 if not found
            if receipt_quantity_val and receipt_quantity_val != "####":
                qty_match = _re.search(r"(\d+(?:\.\d+)?)", receipt_quantity_val)
                if qty_match:
                    qty = float(qty_match.group(1))
                else:
                    try:
                        qty = float(receipt_quantity_val)
                    except:
                        # If parsing fails, default to 0 but still capture the row
                        qty = 0.0
            # If receipt_quantity_val is "####" or empty, qty is already 0.0
            
            # Keep ALL rows including zero quantities - do not skip
            
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

        # Extract from ALL month columns - do NOT skip any, even if empty
        # CRITICAL: Each month column must be extracted independently - empty cells are zero, not missing values
        for mi in month_indices:
            # Get value for this month column - handle empty cells and "####" values
            if mi < len(values):
                qty_raw = values[mi]
            else:
                qty_raw = ""  # Empty if column index out of range
            
            # Handle empty cells, "####" (Excel formatting issue), and other empty indicators
            if not qty_raw or str(qty_raw).strip() in ("", "-", "N/A", "n/a", "None", "####", "nan", "NaN"):
                qty = 0.0  # Treat empty as zero
            else:
                try:
                    if isinstance(qty_raw, str):
                        qty_s = qty_raw.replace(",", "").strip()
                        # Handle "####" which is Excel's way of showing a number that's too wide
                        if qty_s == "####":
                            qty = 0.0  # Treat as zero (could try to read actual value, but safer to use 0)
                        else:
                            qty = float(qty_s) if qty_s else 0.0
                    else:
                        qty = float(qty_raw or 0)
                except Exception:
                    qty = 0.0  # If parsing fails, default to 0
            
            # Keep ALL rows including zero quantities - do not skip
            month_label = columns[mi]
            if debug:
                print(f"[DEBUG] Adding: SKU={material}, {month_label}={qty} (raw={qty_raw})")
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
		"\n"
		"THINK LIKE A HUMAN - UNDERSTAND TABLE STRUCTURE:\n"
		"1. First, understand the table structure by looking at the column headers and data patterns.\n"
		"2. Identify columns by their MEANING and POSITION, not just exact names.\n"
		"3. Columns may have different names in different tables - understand what they represent.\n"
		"\n"
		"INTELLIGENT COLUMN IDENTIFICATION:\n"
		"1. MATERIAL/SKU IDENTIFIER COLUMN:\n"
		"   - Look for columns that contain product codes, SKUs, material IDs, or item identifiers.\n"
		"   - Common names: 'SKU', 'Product Number', 'Material', 'Item Code', 'Product ID', 'Code', 'Part Number', 'Loc' (if it contains product codes).\n"
		"   - If there are multiple identifier columns (e.g., 'Loc' and 'SKU'), prefer the one that looks like a product identifier (often 'SKU' or 'Product Number').\n"
		"   - If 'Loc' contains product codes and 'SKU' contains product codes, use 'SKU' as it's more specific.\n"
		"   - The material ID should be the actual product identifier (e.g., 'ABC', 'DEF', '80116209', '80435665').\n"
		"\n"
		"2. DESCRIPTION COLUMN:\n"
		"   - Look for columns containing product descriptions, item names, or product short descriptions.\n"
		"   - Common names: 'Description', 'Product Description', 'Item Description', 'Product Name', 'Product Short Description'.\n"
		"\n"
		"3. MONTH/DATE COLUMNS:\n"
		"   - Look for columns with month-year format headers like 'Mar-23', 'Apr-23', 'May-23', 'Jun-23', 'Jul-23', 'Aug-23', 'Sep-23', 'Oct-23', 'Nov-23', 'Dec-23', 'Jan-24', 'Feb-24', etc.\n"
		"   - These columns contain quantities for specific months.\n"
		"   - CRITICAL: Count ALL month columns from first to last - do NOT miss any, including the last one (e.g., 'Aug-23').\n"
		"   - Extract from EVERY month column, even if the value is zero.\n"
		"\n"
		"4. COLUMNS TO IGNORE (NOT month columns):\n"
		"   - 'Pending', 'Pending 23-Feb', or any 'Pending' column - this is not a month column.\n"
		"   - 'Batch', 'Batch size', 'Pack Size' - these are not quantities for months.\n"
		"   - 'Order Leadtime', 'Lead Time', 'Leadtime' - this is a time duration, not a quantity.\n"
		"   - 'Receipt EI', 'Receipt Qty' (if it's not a month column) - understand context.\n"
		"   - Any column that is clearly not a month-year format.\n"
		"\n"
		"CRITICAL: Extract ALL quantities (INCLUDING ZERO) from ONLY the month/date columns for EVERY row with a valid material code. "
		"Month columns are those with headers like 'Mar-23', 'Apr-23', 'May-23', 'Jun-23', 'Jul-23', 'Aug-23', etc. "
		"Do NOT extract from 'Pending', 'Batch size', or other non-month columns. "
		"\n"
		"For each row with a material code, create a separate requirement entry for EVERY month column, EVEN IF THE VALUE IS ZERO OR EMPTY. "
		"IMPORTANT: If a month column shows '0', is blank/empty, or contains no value, you MUST still create an entry with quantity=0 for that month. "
		"CRITICAL: Store zero values as quantity=0 - do NOT skip them, do NOT ignore them, do NOT omit them. "
		"CRITICAL: If a cell is empty, treat it as quantity=0 - do NOT use the value from the previous or next month column. "
		"CRITICAL: Do NOT shift values between months - if a cell is empty, it means quantity=0 for that specific month, NOT the value from another month. "
		"Every month column must have an entry, even if the value is zero or empty. "
		"\n"
		"CRITICAL: Do NOT miss the last month column (e.g., 'Aug-23'). "
		"Count the month columns in the table header, then ensure you extract from ALL of them. "
		"If the table has 6 month columns (Mar-23 through Aug-23), you MUST create 6 entries per row. "
		"If the table has 12 month columns, you MUST create 12 entries per row. "
		"Do NOT stop early - extract from the FIRST month column through the LAST month column. "
		"\n"
		"EXTRACTION METHOD - THINK LIKE A HUMAN:\n"
		"1. Read the table systematically, row by row, from top to bottom.\n"
		"2. For each data row:\n"
		"   a. Identify the material/SKU identifier (from the appropriate column based on meaning, not just name).\n"
		"   b. Identify the description (if available).\n"
		"   c. Scan across ALL columns to find month columns (those with month-year headers).\n"
		"   d. For EACH month column found, extract the value in that column for this row.\n"
		"   e. Create one entry per month column, even if the value is zero.\n"
		"\n"
		"3. COLUMN ALIGNMENT - CRITICAL:\n"
		"   - Read the table structure first - understand which columns are which.\n"
		"   - Match values to their column headers by POSITION, not by guessing.\n"
		"   - If a value is in the column with header 'Mar-23', it belongs to Mar-23.\n"
		"   - If a value is in the column with header 'Aug-23', it belongs to Aug-23.\n"
		"   - Do NOT shift values between columns - match each value to its correct column header.\n"
		"   - CRITICAL: If a cell is empty or blank, it means quantity=0 for that specific month - do NOT use the value from the previous or next month column.\n"
		"   - CRITICAL: Each month column must be extracted independently - empty cells are zero, not missing values to fill from other months.\n"
		"\n"
		"4. COMPLETE EXTRACTION:\n"
		"   - Count the month columns in the header row.\n"
		"   - For each data row, extract from ALL month columns you counted.\n"
		"   - Do NOT stop early - extract from the first month column through the last month column.\n"
		"   - If you see 6 month columns (e.g., Mar-23 through Aug-23), extract all 6 for each row.\n"
		"   - If you see 12 month columns, extract all 12 for each row.\n"
		"\n"
		"5. EXAMPLE - Understanding table structure:\n"
		"   Table: 'Loc | SKU | Description | Batch size | Pending | Mar-23 | Apr-23 | May-23 | Jun-23 | Jul-23 | Aug-23'\n"
		"   Row: '5027 | ABC | ABC | 50,000 | 0 | 5 | 75 | 10 | 1 | 2 | 4'\n"
		"   Analysis:\n"
		"   - Column 'Loc' (5027) = location code, NOT material ID\n"
		"   - Column 'SKU' (ABC) = product identifier, USE THIS as material ID\n"
		"   - Column 'Description' (ABC) = product description\n"
		"   - Column 'Batch size' (50,000) = batch information, NOT a month column\n"
		"   - Column 'Pending' (0) = pending quantity, NOT a month column\n"
		"   - Columns 'Mar-23' through 'Aug-23' = month columns, extract from ALL 6\n"
		"   Extract:\n"
		"   - material=ABC (from SKU), quantity=5, date=2023-03-01 (from Mar-23 column)\n"
		"   - material=ABC, quantity=75, date=2023-04-01 (from Apr-23 column)\n"
		"   - material=ABC, quantity=10, date=2023-05-01 (from May-23 column)\n"
		"   - material=ABC, quantity=1, date=2023-06-01 (from Jun-23 column)\n"
		"   - material=ABC, quantity=2, date=2023-07-01 (from Jul-23 column)\n"
		"   - material=ABC, quantity=4, date=2023-08-01 (from Aug-23 column) - DO NOT MISS THIS\n"
		"\n"
		"IMPORTANT: When extracting dates, use the EXACT month label from the column header (e.g., 'Mar-23' -> '2023-03-01', 'Apr-23' -> '2023-04-01'). "
		"Map each quantity to its CORRECT month column based on the column header directly above it. "
		"\n"
		"If material code contains additional text (e.g., '59432479 Alloga UK', '66800015 Japan'), extract just the numeric code part (e.g., '59432479', '66800015'). "
		"Do NOT extract total rows, summary rows, header rows, or rows without material/product ID/SKU. "
		"Do NOT extract rows that are just customer names or headings without material/quantity data. "
		"Return ONLY JSON (no markdown) as a list of objects with keys: "
		"customer (customer name if found in row, otherwise empty), material (use the product identifier from the appropriate column - could be 'SKU', 'Product Number', 'Material', etc. - use the column that contains the actual product identifier, NOT location codes), quantity, unit, delivery_date (date in YYYY-MM-DD format), description (product/item description if available), urgency, notes, source, source_file, row_index. "
		"For the material field, intelligently identify which column contains the product identifier:\n"
		"   - If there's a 'SKU' column, use that value (e.g., 'ABC', 'DEF').\n"
		"   - If there's a 'Product Number' or 'Material' column, use that value.\n"
		"   - If there are both 'Loc' and 'SKU' columns, use 'SKU' (it's more specific to products).\n"
		"   - If 'Loc' is the only identifier column and contains product codes, you may use it.\n"
		"   - The material ID should be the actual product identifier (e.g., 'ABC', 'DEF', '80116209', '80435665').\n"
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
		"THINK LIKE A HUMAN - UNDERSTAND THE TABLE STRUCTURE FIRST. "
		"\n"
		"INTELLIGENT TABLE UNDERSTANDING:\n"
		"1. First, examine the table structure - look at column headers and understand what each column represents.\n"
		"2. Column names may vary - identify columns by their MEANING and CONTEXT, not just exact names.\n"
		"3. Understand the table layout - identify:\n"
		"   - Product identifier columns (SKU, Product Number, Material, etc.)\n"
		"   - Description columns\n"
		"   - Quantity columns (month columns, Receipt Quantity, etc.)\n"
		"   - Date columns (Delivery Date, month columns, etc.)\n"
		"   - Other informational columns (Remarks, Comments, etc.)\n"
		"\n"
		"CRITICAL EXTRACTION RULES:\n"
		"1. You MUST extract EVERY SINGLE data row from the table - no exceptions, no omissions.\n"
		"2. Count the data rows in the image first, then ensure your JSON has the EXACT same number of entries.\n"
		"3. Include ALL rows that contain a valid product number/material ID/SKU (even if quantity is 0 or blank).\n"
		"4. Do NOT skip rows, do NOT truncate, do NOT stop early.\n"
		"5. If you see a product number (like 80435665, 80116209, ABC, DEF), you MUST include that entire row with all its data.\n"
		"\n"
		"INTELLIGENT COLUMN IDENTIFICATION:\n"
		"1. MATERIAL/PRODUCT IDENTIFIER:\n"
		"   - Look for columns containing product codes, SKUs, material IDs.\n"
		"   - Common names: 'Product Number', 'SKU', 'Material', 'Item Code', 'Product ID', 'Code', 'Part Number'.\n"
		"   - If both 'Loc' and 'SKU' exist, typically 'SKU' is the product identifier.\n"
		"\n"
		"2. QUANTITY COLUMNS:\n"
		"   - Month columns: Headers like 'Mar-23', 'Apr-23', 'May-23', 'Jun-23', 'Jul-23', 'Aug-23', etc.\n"
		"   - Receipt Quantity: Column named 'Receipt Quantity', 'Quantity', 'Order Quantity', 'Qty', or similar.\n"
		"   - DO NOT confuse 'Order Leadtime' (time duration like '180 days') with quantity.\n"
		"   - DO NOT use 'Batch size' or 'Pack Size' as quantity.\n"
		"   - Quantity should be a number with units (e.g., '1 PCE', '2 PCE', '10 PCE', '100', '50 KG').\n"
		"\n"
		"3. DATE COLUMNS:\n"
		"   - 'Delivery Date' column with specific dates.\n"
		"   - Month columns (Mar-23, Apr-23, etc.) represent months.\n"
		"\n"
		"EXTRACTION FORMATS:\n"
		"- If the table has 'Delivery Date' and 'Receipt Quantity' columns: extract ONE JSON row per data row.\n"
		"- Map: Product identifier -> material, Delivery Date -> delivery_date, Receipt Quantity -> quantity.\n"
		"- CRITICAL: If a cell is empty or blank, treat it as quantity=0 - do NOT skip it, do NOT use value from another column.\n"
		"- If the table has month columns (e.g., Mar-23, Apr-23): emit ONE JSON row per month value (including zero and empty) for each SKU.\n"
		"- CRITICAL: Count ALL month columns and extract from ALL of them - do NOT miss any, including the last one.\n"
		"- CRITICAL: If a month cell is empty or blank, extract it as quantity=0 for that specific month - do NOT use the value from the previous or next month.\n"
		"- CRITICAL: Do NOT shift values between months - each month column must be extracted independently.\n"
		"\n"
		"EXCLUSION RULES:\n"
		"- Do NOT extract total rows, summary rows, header rows, or rows without material/product ID/SKU.\n"
		"- Do NOT extract rows that are just customer names or headings without material/quantity data.\n"
		"- If 'Remarks' contains 'Dropped', skip that SKU.\n"
		"- Include ALL rows with valid product numbers, even if quantity is 0 or blank.\n"
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
