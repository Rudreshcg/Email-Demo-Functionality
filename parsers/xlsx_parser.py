from typing import Dict, List, Any
from io import BytesIO
import pandas as pd


def _drop_summary_rows(df: pd.DataFrame) -> pd.DataFrame:
	# Identify likely ID columns (case-insensitive, with variations)
	id_candidates = []
	for c in df.columns:
		c_lower = str(c).strip().lower()
		if any(keyword in c_lower for keyword in ["supplier part", "item number", "item", "sku", "material", "code", "part #", "part number"]):
			id_candidates.append(c)
	
	# Identify month columns by pattern
	import re as _re
	# Handle both abbreviated and full month names (e.g., "June", "July", "Sept")
	# Also handle formats like "1-May-23", "1-.1", "23-Mar", "23-Apr" (date columns that might be month indicators)
	month_cols = [c for c in df.columns if _re.match(r"^(\d+[-.])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$", str(c), _re.IGNORECASE)]
	if not month_cols:
		return df

	def is_summary_row(row) -> bool:
		# All id columns empty or whitespace
		ids_empty = True
		for c in id_candidates:
			val = row.get(c, "")
			s = ("" if pd.isna(val) else str(val)).strip()
			if s and s not in ("-", "N/A", "n/a", "None", ""):
				ids_empty = False
				break
		if not ids_empty:
			return False
		# If no ID columns exist, check if row has month values (likely total row)
		# At least one month column has numeric values
		numeric_months = 0
		for c in month_cols:
			val = row.get(c, None)
			if pd.isna(val):
				continue
			try:
				s = str(val).replace(",", "").replace("-", "").strip()
				if s:
					# Check if it's a valid number
					num_val = float(s)
					if num_val > 0:  # Only count positive numbers
						numeric_months += 1
			except Exception:
				continue
		# If no ID but has any numeric month values, it's likely a total row
		return numeric_months >= 1

	mask = df.apply(lambda r: is_summary_row(r.to_dict()), axis=1)
	return df[~mask]


def _find_header_row(df: pd.DataFrame) -> int:
	"""Find the row index that contains the actual data headers (Description, Item Code, month columns)."""
	import re as _re
	from datetime import datetime
	# Handle both abbreviated and full month names (e.g., "June", "July", "Sept")
	# Also handle formats like "1-May-23", "1-.1", "23-Mar", "23-Apr" (date columns that might be month indicators)
	month_pattern = _re.compile(r"^(\d+[-.])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$", _re.IGNORECASE)
	date_pattern = _re.compile(r"^\d{4}-\d{2}-\d{2}")  # Matches dates like "2023-03-05"
	
	# Look for a row that has both an ID column and date/month columns
	for idx, row in df.iterrows():
		row_strs = [str(v).strip() for v in row.values if pd.notna(v)]
		# Check if this row has month-like columns (Mar-23 format) OR date columns (2023-03-05 format)
		month_cols_found = sum(1 for v in row_strs if month_pattern.match(str(v)))
		date_cols_found = sum(1 for v in row_strs if date_pattern.match(str(v)))
		# Check if this row has ID column indicators
		id_cols_found = any(
			any(keyword in str(v).lower() for keyword in ["item code", "item number", "item", "sku", "material", "code", "description"])
			for v in row_strs
		)
		# Found header row if it has ID column and (month columns OR date columns)
		if id_cols_found and (month_cols_found >= 3 or date_cols_found >= 3):
			return idx
	return 0  # Default to first row if not found


def read_xlsx_bytes(xlsx_bytes: bytes) -> List[Dict[str, Any]]:
	# Read from bytes using pandas - first read without headers to find the right header row
	# Create BytesIO once and reuse it
	dfs = pd.read_excel(BytesIO(xlsx_bytes), engine="openpyxl", sheet_name=None, header=None)
	records: List[Dict[str, Any]] = []
	import re as _re
	# Handle both abbreviated and full month names (e.g., "June", "July", "Sept")
	# Also handle formats like "1-May-23", "1-.1", "23-Mar", "23-Apr" (date columns that might be month indicators)
	month_pattern = _re.compile(r"^(\d+[-.])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$", _re.IGNORECASE)
	
	for sheet_name, df in dfs.items():
		if df is None or df.empty:
			continue
		# Search for header row more aggressively - check first 10 rows
		import re as _re_date
		date_pattern = _re_date.compile(r"^\d{4}-\d{2}-\d{2}")  # Matches dates like "2023-03-05"
		header_row = None
		for idx in range(min(10, len(df))):  # Check first 10 rows
			row_values = [str(v).strip() for v in df.iloc[idx].values if pd.notna(v)]
			month_count = sum(1 for v in row_values if month_pattern.match(str(v)))
			date_count = sum(1 for v in row_values if date_pattern.match(str(v)))
			id_found = any(any(kw in str(v).lower() for kw in ["item code", "description"]) for v in row_values)
			# Found header if it has ID column and (month columns OR date columns)
			if id_found and (month_count >= 5 or date_count >= 5):
				header_row = idx
				break
		
		if header_row is None:
			header_row = _find_header_row(df)  # Fallback to original method
		
		# Re-read with the correct header row - create new BytesIO for each read
		df_with_headers = pd.read_excel(BytesIO(xlsx_bytes), engine="openpyxl", sheet_name=sheet_name, header=header_row)
		# Normalize headers to strings
		df_with_headers.columns = [str(c).strip() for c in df_with_headers.columns]
		
		# Convert date column headers to month format (e.g., "2023-03-05 00:00:00" -> "Mar-23")
		new_columns = []
		for col in df_with_headers.columns:
			col_str = str(col).strip()
			# Check if it's a date format column
			if date_pattern.match(col_str):
				try:
					# Parse date and convert to month format
					from datetime import datetime
					dt = pd.to_datetime(col_str)
					month_name = dt.strftime('%b')  # Mar, Apr, etc.
					year_short = dt.strftime('%y')  # 23, 24, etc.
					new_columns.append(f"{month_name}-{year_short}")
				except:
					new_columns.append(col_str)
			else:
				new_columns.append(col_str)
		df_with_headers.columns = new_columns
		
		# Drop fully empty rows
		df_with_headers = df_with_headers.dropna(how="all")
		# Drop likely summary/total rows
		df_with_headers = _drop_summary_rows(df_with_headers)
		# Add sheet name context
		df_with_headers["__sheet_name"] = sheet_name
		records.extend(df_with_headers.to_dict(orient="records"))
	return records


def read_xlsx_file(path: str) -> List[Dict[str, Any]]:
	# Read without headers first to find the right header row
	dfs = pd.read_excel(path, engine="openpyxl", sheet_name=None, header=None)
	records: List[Dict[str, Any]] = []
	import re as _re
	# Handle both abbreviated and full month names (e.g., "June", "July", "Sept")
	# Also handle formats like "1-May-23", "1-.1", "23-Mar", "23-Apr" (date columns that might be month indicators)
	month_pattern = _re.compile(r"^(\d+[-.])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$", _re.IGNORECASE)
	
	for sheet_name, df in dfs.items():
		if df is None or df.empty:
			continue
		# Search for header row more aggressively - check first 10 rows
		import re as _re_date
		date_pattern = _re_date.compile(r"^\d{4}-\d{2}-\d{2}")  # Matches dates like "2023-03-05"
		header_row = None
		for idx in range(min(10, len(df))):  # Check first 10 rows
			row_values = [str(v).strip() for v in df.iloc[idx].values if pd.notna(v)]
			month_count = sum(1 for v in row_values if month_pattern.match(str(v)))
			date_count = sum(1 for v in row_values if date_pattern.match(str(v)))
			id_found = any(any(kw in str(v).lower() for kw in ["item code", "description"]) for v in row_values)
			# Found header if it has ID column and (month columns OR date columns)
			if id_found and (month_count >= 5 or date_count >= 5):
				header_row = idx
				break
		
		if header_row is None:
			header_row = _find_header_row(df)  # Fallback to original method
		
		# Re-read with the correct header row
		df_with_headers = pd.read_excel(path, engine="openpyxl", sheet_name=sheet_name, header=header_row)
		df_with_headers.columns = [str(c).strip() for c in df_with_headers.columns]
		
		# Convert date column headers to month format (e.g., "2023-03-05 00:00:00" -> "Mar-23")
		new_columns = []
		for col in df_with_headers.columns:
			col_str = str(col).strip()
			# Check if it's a date format column
			if date_pattern.match(col_str):
				try:
					# Parse date and convert to month format
					from datetime import datetime
					dt = pd.to_datetime(col_str)
					month_name = dt.strftime('%b')  # Mar, Apr, etc.
					year_short = dt.strftime('%y')  # 23, 24, etc.
					new_columns.append(f"{month_name}-{year_short}")
				except:
					new_columns.append(col_str)
			else:
				new_columns.append(col_str)
		df_with_headers.columns = new_columns
		
		df_with_headers = df_with_headers.dropna(how="all")
		df_with_headers = _drop_summary_rows(df_with_headers)
		df_with_headers["__sheet_name"] = sheet_name
		records.extend(df_with_headers.to_dict(orient="records"))
	return records
