from typing import Dict, List, Any
from io import BytesIO
import pandas as pd


def _drop_summary_rows(df: pd.DataFrame) -> pd.DataFrame:
	# Identify likely ID columns
	id_candidates = [c for c in df.columns if str(c).strip().lower() in ("supplier part #", "item number", "item", "sku", "material", "code")]
	# Identify month columns by pattern
	import re as _re
	month_cols = [c for c in df.columns if _re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z\-']*\s?\d{2,4}$", str(c), _re.IGNORECASE)]
	if not month_cols:
		return df

	def is_summary_row(row) -> bool:
		# All id columns empty
		ids_empty = True
		for c in id_candidates:
			val = row.get(c, "")
			s = ("" if pd.isna(val) else str(val)).strip()
			if s:
				ids_empty = False
				break
		if not ids_empty:
			return False
		# At least two month columns have numeric values
		numeric_months = 0
		for c in month_cols:
			val = row.get(c, None)
			if pd.isna(val):
				continue
			try:
				float(str(val).replace(",", "").strip())
				numeric_months += 1
			except Exception:
				continue
		return numeric_months >= 2

	mask = df.apply(lambda r: is_summary_row(r.to_dict()), axis=1)
	return df[~mask]


def read_xlsx_bytes(xlsx_bytes: bytes) -> List[Dict[str, Any]]:
	# Read from bytes using pandas
	dfs = pd.read_excel(BytesIO(xlsx_bytes), engine="openpyxl", sheet_name=None)
	records: List[Dict[str, Any]] = []
	for sheet_name, df in dfs.items():
		if df is None or df.empty:
			continue
		# Normalize headers to strings
		df.columns = [str(c).strip() for c in df.columns]
		# Drop fully empty rows
		df = df.dropna(how="all")
		# Drop likely summary/total rows
		df = _drop_summary_rows(df)
		# Add sheet name context
		df["__sheet_name"] = sheet_name
		records.extend(df.to_dict(orient="records"))
	return records


def read_xlsx_file(path: str) -> List[Dict[str, Any]]:
	dfs = pd.read_excel(path, engine="openpyxl", sheet_name=None)
	records: List[Dict[str, Any]] = []
	for sheet_name, df in dfs.items():
		if df is None or df.empty:
			continue
		df.columns = [str(c).strip() for c in df.columns]
		df = df.dropna(how="all")
		df = _drop_summary_rows(df)
		df["__sheet_name"] = sheet_name
		records.extend(df.to_dict(orient="records"))
	return records
