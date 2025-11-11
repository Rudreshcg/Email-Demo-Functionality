import re
from typing import Any, Dict, List

import pandas as pd
from bs4 import BeautifulSoup

_MONTH_PATTERN = re.compile(
	r"^(\d{1,2}[-./])?(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)[a-zA-Z\-']*\s?\d{2,4}$",
	re.IGNORECASE,
)
_MONTH_PATTERN_LOOSE = re.compile(
	r"(Jan(uary)?|Feb(ruary)?|Mar(ch)?|Apr(il)?|May|Jun(e)?|Jul(y)?|Aug(ust)?|Sep(t(ember)?)?|Oct(ober)?|Nov(ember)?|Dec(ember)?)\s*[\'\-/.,]?\s*\d{2,4}",
	re.IGNORECASE,
)
_MONTH_SUFFIX_KEYWORDS = {
	"qty",
	"quantity",
	"quantities",
	"forecast",
	"fcst",
	"volume",
	"vol",
	"units",
	"unit",
	"demand",
	"plan",
	"planning",
	"projection",
	"proj",
	"rolling",
	"commit",
	"committed",
	"target",
	"budget",
	"need",
	"needs",
	"requirement",
	"requirements",
	"req",
	"reqs",
	"sales",
	"shipments",
	"shipment",
	"supply",
	"delivery",
	"dispatch",
	"inventory",
	"stock",
	"balance",
	"consumption",
	"usage",
	"backlog",
}

_HEADER_KEYWORDS = {"sku", "description", "batch", "pending", "loc", "location", "material"}
_HEADER_PRIORITY_KEYWORDS = {
	"sku",
	"product",
	"material",
	"item",
	"code",
	"description",
	"desc",
	"date",
	"delivery",
	"receipt",
	"quantity",
	"qty",
	"unit",
	"measure",
	"category",
	"order",
	"lead",
	"leadtime",
	"supplier",
	"bayer",
	"change",
	"mdat",
	"information",
	"comments",
	"remark",
}


def _strip_month_keywords(text: str) -> str:
	if not text:
		return ""
	tokens = re.split(r"\s+", str(text).strip())
	while tokens:
		last = tokens[-1].strip("()[]{}.,:")
		if last.lower() in _MONTH_SUFFIX_KEYWORDS:
			tokens.pop()
			continue
		break
	while tokens:
		first = tokens[0].strip("()[]{}.,:")
		if first.lower() in _MONTH_SUFFIX_KEYWORDS:
			tokens = tokens[1:]
			continue
		break
	stripped = " ".join(tokens).strip()
	return stripped if stripped else str(text).strip()


def _normalize_month_header(text: str) -> str:
	cleaned = _strip_month_keywords(text)
	return cleaned.rstrip(":").strip()


def _is_month_header(text: str) -> bool:
	if not text:
		return False
	cleaned = str(text).strip()
	if _MONTH_PATTERN.match(cleaned):
		return True
	normalized = _normalize_month_header(cleaned)
	if normalized != cleaned and _MONTH_PATTERN.match(normalized):
		return True
	if _MONTH_PATTERN_LOOSE.search(cleaned):
		return True
	return False


def _flatten_column_name(col: Any, index: int) -> str:
	"""Flatten pandas column names (handle MultiIndex and 'Unnamed:' labels)."""
	if isinstance(col, tuple):
		parts = []
		for part in col:
			if part is None:
				continue
			part_str = str(part).strip()
			if not part_str or part_str.lower().startswith("unnamed"):
				continue
			parts.append(part_str)
		if not parts:
			return f"column_{index}"
		for candidate in parts:
			lower = candidate.lower()
			if any(keyword in lower for keyword in _HEADER_PRIORITY_KEYWORDS):
				return candidate
		# Prefer the last part if it looks like a month label; otherwise join all parts
		for candidate in reversed(parts):
			if _is_month_header(candidate):
				return _normalize_month_header(candidate)
		return " ".join(parts)
	else:
		col_str = str(col).strip()
		if not col_str or col_str.lower().startswith("unnamed"):
			return f"column_{index}"
		return col_str


def _dedupe_columns(columns: List[str]) -> List[str]:
	"""Ensure column names are unique by appending suffixes where needed."""
	seen = {}
	result = []
	for name in columns:
		if name not in seen:
			seen[name] = 1
			result.append(name)
		else:
			count = seen[name]
			seen[name] = count + 1
			result.append(f"{name}_{count}")
	return result


def _should_promote_first_row(columns: List[str], first_row: pd.Series) -> bool:
	if not len(first_row):
		return False
	# Heuristic: many columns look generic (column_X or digits) and first row contains header-like keywords
	generic_cols = sum(
		1
		for name in columns
		if not name
		or name.lower().startswith("column_")
		or name.isdigit()
		or name.lower().startswith("unnamed")
	)
	if generic_cols < max(3, len(columns) // 2):
		return False

	header_like = 0
	for value in first_row.tolist():
		if not isinstance(value, str):
			continue
		text = value.strip()
		if not text:
			continue
		if _is_month_header(text):
			header_like += 1
			continue
		lower = text.lower()
		if any(keyword in lower for keyword in _HEADER_KEYWORDS):
			header_like += 1
			continue
		# Short alphabetic tokens (2-20 chars) are likely headers (e.g., Loc, SKU, ABC)
		if lower.isalpha() and 2 <= len(lower) <= 20:
			header_like += 1
	return header_like >= max(3, len(columns) // 3)


def _score_columns(columns: List[str]) -> int:
	score = 0
	for name in columns:
		lower = name.lower()
		if _is_month_header(name):
			score += 5
		if any(keyword in lower for keyword in _HEADER_KEYWORDS):
			score += 3
		if lower.startswith("column_") or lower.isdigit() or lower.startswith("unnamed"):
			score -= 2
		else:
			score += 1
	return score


def extract_tables_from_html(html: str) -> List[Dict[str, Any]]:
	"""Parse HTML tables into normalized grid dicts compatible with expand_grid_to_requirements."""
	if not html:
		return []

	grids: List[Dict[str, Any]] = []
	soup = BeautifulSoup(html, "lxml")
	for table in soup.find_all("table"):
		table_html = str(table)
		candidates = []
		for header in (0, 1, [0, 1]):
			try:
				df_candidate = pd.read_html(table_html, header=header)[0]
			except ValueError:
				continue
			except Exception:
				continue
			df_candidate = df_candidate.copy()
			if isinstance(df_candidate.columns, pd.MultiIndex):
				flat_cols = [
					_flatten_column_name(col, idx) for idx, col in enumerate(df_candidate.columns)
				]
			else:
				flat_cols = [
					_flatten_column_name(col, idx) for idx, col in enumerate(df_candidate.columns)
				]
			score = _score_columns(flat_cols)
			candidates.append((score, df_candidate, flat_cols))
		if not candidates:
			continue
		# Pick the dataframe with the highest header score
		best_score, df, flat_cols = max(candidates, key=lambda item: item[0])
		df.columns = _dedupe_columns(flat_cols)

		if df.empty:
			continue

		# Reset index to turn it into columns if needed
		if isinstance(df.index, pd.MultiIndex):
			df = df.reset_index()
		else:
			df = df.reset_index(drop=True)

		if isinstance(df.columns, pd.MultiIndex):
			df.columns = [
				_flatten_column_name(col, idx) for idx, col in enumerate(df.columns)
			]
		else:
			df.columns = [
				_flatten_column_name(col, idx) for idx, col in enumerate(df.columns)
			]
		df.columns = _dedupe_columns(list(df.columns))

		# If header row ended up in data, promote it
		if not df.empty and _should_promote_first_row(df.columns.tolist(), df.iloc[0]):
			new_columns = []
			for idx, value in enumerate(df.iloc[0].tolist()):
				if isinstance(value, str) and value.strip():
					new_columns.append(value.strip())
				else:
					new_columns.append(df.columns[idx])
			df = df.iloc[1:].reset_index(drop=True)
			df.columns = _dedupe_columns([_flatten_column_name(col, idx) for idx, col in enumerate(new_columns)])

		# Drop any remaining rows that mirror the header names
		if not df.empty:
			def _row_is_header_like(row: pd.Series) -> bool:
				match_count = 0
				for col_name, value in row.items():
					if not isinstance(value, str):
						continue
					text = value.strip()
					if not text:
						continue
					if text == col_name:
						match_count += 1
					elif _is_month_header(text):
						match_count += 1
				return match_count >= max(3, len(row) // 2)

			header_like_indices = [
				idx for idx, row in df.iterrows() if _row_is_header_like(row)
			]
			if header_like_indices:
				df = df.drop(index=header_like_indices).reset_index(drop=True)

		# Drop rows that are completely empty or NaN
		df = df.dropna(how="all")
		if df.empty:
			continue

		columns = [str(c).strip() for c in df.columns]
		rows: List[Dict[str, Any]] = []
		for _, row in df.iterrows():
			row_dict: Dict[str, Any] = {}
			for col_name, value in zip(columns, row.tolist()):
				if pd.isna(value):
					row_dict[col_name] = ""
				else:
					if isinstance(value, str):
						row_dict[col_name] = value.strip()
					else:
						row_dict[col_name] = value
			rows.append(row_dict)

		if rows:
			grids.append({"columns": columns, "rows": rows})
	return grids

