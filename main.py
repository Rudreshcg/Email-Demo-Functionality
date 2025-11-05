import os
import io
import json
import mimetypes
import re
from typing import List, Dict, Any

import click
import pandas as pd

from bedrock_client import get_bedrock_client
from parsers.eml_parser import parse_eml_bytes
from parsers.xlsx_parser import read_xlsx_file, read_xlsx_bytes
from analysis import analyze_text_requirements, analyze_image_requirements, analyze_image_table_grid, expand_grid_to_requirements, correct_grid_months, refine_projection_with_image
from ingest.imap_fetcher import fetch_emails


SUPPORTED_XLSX_EXTS = {".xlsx"}

# Target email address for order aggregation
TARGET_EMAIL = "orderaggregationdemo@gmail.com"


def is_xlsx_file(path: str) -> bool:
	return os.path.splitext(path)[1].lower() in SUPPORTED_XLSX_EXTS


def should_process_email(parsed_email) -> bool:
	"""Check if email should be processed - must be sent to TARGET_EMAIL"""
	if not parsed_email.recipients:
		return False  # Skip emails without recipients
	# Check if TARGET_EMAIL is in recipients (case-insensitive)
	target_lower = TARGET_EMAIL.lower()
	return any(target_lower in r for r in parsed_email.recipients)


def _coerce_number(value) -> float:
    try:
        if isinstance(value, str):
            v = value.replace(",", "").strip()
            return float(v) if v else 0.0
        return float(value)
    except Exception:
        return 0.0


def _clean_material_code(material: str) -> str:
    """Extract the material code from text that may include additional description.
    Examples: '59432479 Alloga UK' -> '59432479', '66800015 Japan' -> '66800015'
    """
    if not material:
        return ""
    material = material.strip()
    # If it starts with digits, extract the leading numeric part
    import re
    match = re.match(r'^(\d+)', material)
    if match:
        return match.group(1)
    # Otherwise return as-is
    return material


def _extract_customer_from_email(parsed) -> str:
    """Extract customer name from email subject, body, or Excel data.
    Priority: Excel/body customer > subject customer > sender
    Excludes generic phrases like "Material Requirements", "Customer Material Requirements"
    """
    customer = ""
    
    # Generic phrases to exclude (not real customer names)
    generic_phrases = [
        "material requirements", "customer material requirements",
        "material requirement", "customer material requirement",
        "requirements", "material", "customer", "forecast", "test",
        "excel attachment", "excel", "attachment", "table", "table as image",
        "format", "format1", "format2", "image", "file", "document",
        "welcome", "hey", "subject", "re:", "fw:", "fwd:", "test email"
    ]
    
    def is_valid_customer_name(name: str) -> bool:
        """Check if a name is a valid customer name (not generic phrase).
        Valid customer names should look like company names (e.g., "ABC Pvt Ltd", "Cipla", "ENCUBE ETHICALS PVT LTD").
        """
        if not name or len(name) < 3:
            return False
        name_lower = name.lower().strip()
        
        # Check if it's a generic phrase
        if name_lower in generic_phrases:
            return False
        
        # Check if it contains generic phrases
        for phrase in generic_phrases:
            if phrase in name_lower:
                return False
        
        # Should not be just common words
        if name_lower in ["test", "sample", "demo", "example", "table", "image", "format", "attachment"]:
            return False
        
        # Should contain letters (not just numbers or special characters)
        if not any(c.isalpha() for c in name):
            return False
        
        # Should not be just a single word (unless it's a known company indicator)
        words = name.split()
        if len(words) == 1:
            # Single word could be valid if it's a company name (e.g., "Cipla", "GSK")
            # But exclude if it's too short or looks generic
            if len(name) < 4 or name_lower in ["table", "image", "format", "test", "excel", "attachment"]:
                return False
        
        # Should look like a company name - contains letters, may have company indicators
        # Patterns like "ABC Pvt Ltd", "XYZ Inc", "Company Name", etc.
        has_company_indicators = any(word.lower() in ["pvt", "ltd", "limited", "inc", "corp", "corporation", "llc", "llp"] for word in words)
        has_capital_letters = any(c.isupper() for c in name)
        
        # If it has company indicators OR starts with capital letters, more likely to be valid
        if has_company_indicators or (has_capital_letters and len(words) >= 2):
            return True
        
        # If it's just a single short word without company indicators, likely not valid
        if len(words) == 1 and len(name) < 6:
            return False
        
        # Default: if it passes basic checks and has reasonable length, accept it
        return len(name) >= 4 and has_capital_letters
    
    # Check email subject for customer name patterns
    subject = str(parsed.subject or "").strip()
    if subject:
        import re
        # Remove common prefixes like "Material Requirements -", "Excel attachment -", "Table -", etc.
        subject_cleaned = subject
        # Remove generic prefixes from start
        generic_prefixes = [
            r"Material\s+Requirements",
            r"Customer\s+Material\s+Requirements",
            r"Excel\s+attachment",
            r"Table\s+as\s+image",
            r"Table\s+&\s+Format",
            r"Table",
            r"Format",
            r"Image",
            r"Attachment",
            r"Test",
            r"Welcome",
            r"Hey"
        ]
        prefix_pattern = "|".join(generic_prefixes)
        subject_cleaned = re.sub(rf"^({prefix_pattern})\s*[-–—&]\s*", "", subject_cleaned, flags=re.IGNORECASE)
        
        # Look for customer name patterns (must look like company names)
        patterns = [
            r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Pvt|Ltd|Limited|Inc|Corp|Corporation|LLC|LLP))?)\s*[-–—]",  # "Customer Name - ..." or "ABC Pvt Ltd - ..."
            r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Pvt|Ltd|Limited|Inc|Corp|Corporation|LLC|LLP))?)\s*:",  # "Customer Name: ..."
            r"for\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Pvt|Ltd|Limited|Inc|Corp|Corporation|LLC|LLP))?)(?:\s|$|,|\.)",  # "... for Customer Name"
            r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Pvt|Ltd|Limited|Inc|Corp|Corporation|LLC|LLP))?)(?:\s|$)",  # "Customer Name" at start (only if it looks like a real name)
        ]
        
        # Try cleaned subject first
        for pattern in patterns:
            match = re.search(pattern, subject_cleaned, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                if is_valid_customer_name(candidate):
                    customer = candidate
                    break
        if customer:
            return customer
        
        # Try original subject if cleaned didn't work (but be more strict)
        for pattern in patterns:
            match = re.search(pattern, subject, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()
                if is_valid_customer_name(candidate):
                    customer = candidate
                    break
        if customer:
            return customer
    
    # Check email body for customer name (if not found in subject)
    if not customer:
        full_text = "\n\n".join([parsed.plain_text or "", parsed.html_text or ""]).strip()
        if full_text:
            # Look for customer name in first few lines
            lines = full_text.split('\n')[:10]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                import re
                patterns = [
                    r"(?:Customer|Customer Name|Company|Client):\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Pvt|Ltd|Limited|Inc|Corp|Corporation|LLC|LLP))?)(?:\s|$|,|\.)",
                    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*(?:\s+(?:Pvt|Ltd|Limited|Inc|Corp|Corporation|LLC|LLP))?)\s+(?:Material|Requirements|Forecast)",
                ]
                for pattern in patterns:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        candidate = match.group(1).strip()
                        if is_valid_customer_name(candidate):
                            customer = candidate
                            break
                if customer:
                    break
    
    # If still no customer found, return empty (will use sender as fallback later)
    return customer


def _extract_requirements_from_excel_row(rec: Dict[str, Any], bedrock, customer: str, email_customer: str, source: str, source_file: str, row_idx: int, debug: bool = False) -> List[Dict[str, Any]]:
    """Directly extract requirements from an Excel row by programmatically processing month columns.
    This avoids LLM date mapping errors by directly reading month column headers.
    """
    import re as _re
    
    # Identify month columns
    month_pattern = _re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z\-']*\s?\d{2,4}$", _re.IGNORECASE)
    month_cols = []
    other_cols = {}
    
    for k, v in rec.items():
        if v == v:  # filter NaN
            if month_pattern.match(str(k).strip()):
                month_cols.append((str(k).strip(), v))
            else:
                other_cols[str(k).strip()] = v
    
    # Extract material code from other columns
    id_candidates = ["Product Number", "Product ID", "Item Code", "Item Number", "Item", "SKU", "Material", "Code", "Part #", "Part Number", "Supplier Part #", "Product Code"]
    material = ""
    for col_name in id_candidates:
        for k, v in other_cols.items():
            k_lower = str(k).strip().lower()
            col_lower = col_name.lower().strip()
            # More flexible matching - check if column name contains or equals the candidate
            if col_lower in k_lower or k_lower in col_lower or k_lower == col_lower:
                material = str(v).strip() if v == v else ""
                if material and material not in ("-", "N/A", "n/a", "None", ""):
                    material = _clean_material_code(material)
                    if debug and material:
                        print(f"[DEBUG] Found material code '{material}' in column '{k}'")
                    break
        if material:
            break
    
    if not material:
        # If no material code found, try to extract from all non-month columns using LLM
        other_text = ", ".join([f"{k}={v}" for k, v in other_cols.items()])
        if bedrock and other_text:
            if debug:
                print(f"[DEBUG] Row {row_idx}: No material code found in standard columns, trying LLM extraction. Columns: {list(other_cols.keys())}")
            try:
                # Use LLM to extract just the material code
                material_text = analyze_text_requirements(
                    bedrock,
                    user_text=f"Row data: {other_text}. Extract ONLY the material/product ID/code/SKU (numeric part only). If material code has additional text like '59432479 Alloga UK', extract just '59432479'. Return JSON with material field only.",
                    system_text="Extract material code from the row data. Return JSON: {\"material\": \"code\"}",
                    source=source,
                    source_file=source_file,
                    debug=debug,
                )
                if material_text and len(material_text) > 0:
                    material = _clean_material_code(str(material_text[0].get("material", "") or ""))
                    if debug and material:
                        print(f"[DEBUG] LLM extracted material code: '{material}'")
            except Exception as e:
                if debug:
                    print(f"[DEBUG] LLM extraction failed: {e}")
                pass
    
    if not material:
        return []  # Skip rows without material code
    
    # Extract description (product/item description) - will be put in notes field
    description = ""
    description_candidates = ["Item Description", "Description", "Product Description", "Product Name", "Item Name"]
    for col_name in description_candidates:
        for k, v in other_cols.items():
            if col_name.lower() in k.lower():
                description = str(v).strip() if v == v else ""
                if description:
                    break
        if description:
            break
    
    # Extract notes (separate from description)
    notes_text = ""
    notes_candidates = ["Notes", "Note", "Remarks"]
    for col_name in notes_candidates:
        for k, v in other_cols.items():
            if col_name.lower() in k.lower():
                notes_text = str(v).strip() if v == v else ""
                if notes_text:
                    break
        if notes_text:
            break
    
    # Combine description with notes (description goes to notes field like before)
    # If both exist, combine them; otherwise use whichever exists
    final_notes = ""
    if description and notes_text:
        final_notes = f"{description} | {notes_text}"
    elif description:
        final_notes = description
    elif notes_text:
        final_notes = notes_text
    
    # Check for dropped SKUs
    if "dropped" in (final_notes.lower()):
        return []  # Skip dropped SKUs
    
    # Extract customer name if available (priority: Excel row > email subject/body > email sender)
    customer_candidates = ["Customer", "Customer Name", "Company"]
    row_customer = ""
    for col_name in customer_candidates:
        for k, v in other_cols.items():
            if col_name.lower() in k.lower():
                row_customer = str(v).strip() if v == v else ""
                if row_customer:
                    break
        if row_customer:
            break
    
    # Priority: Excel row customer > email subject/body customer > email sender (fallback)
    final_customer = row_customer or email_customer or customer or ""
    
    # Extract unit if available
    unit = ""
    unit_candidates = ["Unit of Measure", "Unit", "Units", "UOM", "Batch size", "Pack Size"]
    for col_name in unit_candidates:
        for k, v in other_cols.items():
            if col_name.lower() in k.lower():
                unit = str(v).strip() if v == v else ""
                if unit:
                    break
        if unit:
            break
    
    # Check if we have "Delivery Date" and "Receipt Quantity" columns (one row per Excel row)
    delivery_date_col = None
    receipt_quantity_col = None
    
    for k in other_cols.keys():
        k_lower = str(k).strip().lower()
        if "delivery date" in k_lower:
            delivery_date_col = k
        if "receipt quantity" in k_lower:
            receipt_quantity_col = k
    
    requirements = []
    
    # If we have Delivery Date and Receipt Quantity columns, extract one requirement per row
    if delivery_date_col and receipt_quantity_col:
        delivery_date_val = str(other_cols.get(delivery_date_col, "")).strip()
        receipt_quantity_val = str(other_cols.get(receipt_quantity_col, "")).strip()
        
        if delivery_date_val and receipt_quantity_val:
            # Extract numeric quantity from "Receipt Quantity" (e.g., "1 PCE" -> 1)
            qty_str = receipt_quantity_val
            # Try to extract numeric part
            qty_match = _re.search(r"(\d+(?:\.\d+)?)", qty_str)
            if qty_match:
                qty = _coerce_number(qty_match.group(1))
            else:
                qty = _coerce_number(receipt_quantity_val)
            
            # If unit wasn't found in "Unit of Measure", try to extract from "Receipt Quantity"
            if not unit and receipt_quantity_val:
                unit_match = _re.search(r"\d+\s*([A-Za-z]+)", receipt_quantity_val)
                if unit_match:
                    unit = unit_match.group(1).strip()
            
            if qty > 0:
                # Normalize date to ISO format (YYYY-MM-DD)
                from analysis import _normalize_date_to_iso
                delivery_date_iso = _normalize_date_to_iso(delivery_date_val)
                
                requirements.append({
                    "customer": final_customer,
                    "material": material,
                    "quantity": qty,
                    "unit": unit,
                    "delivery_date": delivery_date_iso,
                    "urgency": "",
                    "description": description,
                    "notes": final_notes,
                    "source": source,
                    "source_file": source_file,
                    "row_index": row_idx,
                })
                if debug:
                    print(f"[DEBUG] Row {row_idx}: Extracted requirement - material={material}, quantity={qty}, unit={unit}, delivery_date={delivery_date_iso}")
    
    # Also extract requirements from month columns (if any)
    for month_col, qty_val in month_cols:
        qty = _coerce_number(qty_val)
        if qty <= 0:
            continue
        
        # Normalize month label to ISO date (YYYY-MM-DD)
        from analysis import _normalize_date_to_iso
        delivery_date = _normalize_date_to_iso(month_col)
        
        requirements.append({
            "customer": final_customer,
            "material": material,
            "quantity": qty,
            "unit": unit,
            "delivery_date": delivery_date,
            "urgency": "",
            "description": description,  # Keep description field for reference
            "notes": final_notes,  # Put description in notes field (like before)
            "source": source,
            "source_file": source_file,
            "row_index": row_idx,
        })
        if debug:
            print(f"[DEBUG] Row {row_idx}: Extracted requirement from month column - material={material}, quantity={qty}, month={month_col}, delivery_date={delivery_date}")
    
    return requirements


def _sanitize_rows(rows: List[Dict[str, Any]], customer: str, source: str, source_file: str) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in rows or []:
        qty = _coerce_number(item.get("quantity", 0))
        # drop zero or negative quantities
        if qty <= 0:
            continue
        # skip rows without material/item number (likely total/summary rows)
        material = str(item.get("material", "") or item.get("id", "") or "").strip()
        if not material:
            continue
        # Clean material code to extract just the numeric part if it has additional text
        material = _clean_material_code(material)
        if not material:
            continue
        notes = str(item.get("notes", "") or "")
        description = str(item.get("description", "") or "")
        
        # Combine description with notes (description should appear in notes field)
        # If both exist, combine them; otherwise use whichever exists
        final_notes = ""
        if description and notes:
            final_notes = f"{description} | {notes}"
        elif description:
            final_notes = description
        elif notes:
            final_notes = notes
        
        # skip dropped SKUs
        if "dropped" in final_notes.lower():
            continue
        out = dict(item)
        out["quantity"] = qty
        out["material"] = material  # Use cleaned material code
        if "id" in out:
            out["id"] = material
        # Use customer from row if available, otherwise use email sender (passed customer parameter)
        # This ensures consistent customer name across all rows from the same email
        row_customer = str(item.get("customer", "") or "").strip()
        if row_customer:
            out["customer"] = row_customer
        else:
            out["customer"] = customer or ""
        # Put description in notes field (like Excel does)
        out["notes"] = final_notes
        out["description"] = description  # Keep description field for reference
        
        # Normalize delivery_date to ISO format (YYYY-MM-DD) for consistent pivot tables
        delivery_date = str(item.get("delivery_date", "") or "").strip()
        if delivery_date:
            from analysis import _normalize_date_to_iso
            # Normalize any date format to ISO format (YYYY-MM-DD)
            out["delivery_date"] = _normalize_date_to_iso(delivery_date)
        
        out["source"] = source
        out["source_file"] = source_file
        cleaned.append(out)
    return cleaned


@click.command()
@click.option("--input-dir", required=False, default=None, type=click.Path(exists=True, file_okay=False), help="Folder containing .xlsx files")
@click.option("--out", required=False, default="requirements_output.xlsx", type=click.Path(dir_okay=False), help="Output Excel path")
@click.option("--region", required=False, default="us-east-1", help="AWS region")
@click.option("--dry-run", is_flag=True, help="Do not call Bedrock; only parse")
@click.option("--max-files", required=False, default=0, type=int, help="Limit number of files (0 = unlimited)")
@click.option("--imap-host", required=False, default=None, help="IMAP host (e.g., imap.gmail.com)")
@click.option("--imap-user", required=False, default=None, help="IMAP username/email")
@click.option("--imap-pass", required=False, default=None, help="IMAP password or app password")
@click.option("--imap-mailbox", required=False, default="INBOX", help="IMAP mailbox to read")
@click.option("--imap-criteria", required=False, default="ALL", help="IMAP search criteria, e.g., UNSEEN or ALL or SINCE 01-Oct-2025")
@click.option("--imap-limit", required=False, default=5, type=int, help="Max number of IMAP emails to fetch (fetches most recent)")
@click.option("--debug", is_flag=True, help="Print verbose debug info and raw model outputs")

def main(input_dir: str, out: str, region: str, dry_run: bool, max_files: int, imap_host: str, imap_user: str, imap_pass: str, imap_mailbox: str, imap_criteria: str, imap_limit: int, debug: bool):
	bedrock = None if dry_run else get_bedrock_client(region)
	all_rows: List[Dict[str, Any]] = []

	processed = 0

	# IMAP path if configured
	if imap_host and imap_user and imap_pass:
		print(f"IMAP: connecting to {imap_host} mailbox={imap_mailbox} criteria=\"{imap_criteria}\" limit={imap_limit}")
		emails = fetch_emails(
			host=imap_host,
			username=imap_user,
			password=imap_pass,
			mailbox=imap_mailbox,
			criteria=imap_criteria,
			limit=imap_limit,
		)
		print(f"IMAP: fetched {len(emails)} message(s)")
		for raw_bytes, uid in emails:
			if max_files and processed >= max_files:
				break
			print(f"IMAP: processing UID {uid} ...")
			rows = handle_eml_bytes(raw_bytes, uid, bedrock, dry_run, debug)
			print(f"IMAP: UID {uid} -> extracted {len(rows)} row(s)")
			all_rows.extend(rows)
			processed += 1

	# Filesystem path
	if input_dir:
		print(f"FILES: scanning {input_dir}")
		files = [os.path.join(input_dir, f) for f in os.listdir(input_dir)]
		for path in files:
			if max_files and processed >= max_files:
				break
			if os.path.isdir(path):
				continue
			if is_xlsx_file(path):
				print(f"FILES: parsing xlsx {os.path.basename(path)}")
				rows = handle_xlsx_file(path, bedrock, dry_run)
				print(f"FILES: {os.path.basename(path)} -> extracted {len(rows)} row(s)")
				all_rows.extend(rows)
				processed += 1

	if not all_rows:
		print("No requirements extracted.")
		return

	df = pd.DataFrame(all_rows)
	
	# Map fields to match requirements: customer name, ID, quantity, date
	# Map old field names to new ones if they exist
	if "customer" in df.columns:
		df["customer_name"] = df["customer"]
	if "material" in df.columns:
		df["id"] = df["material"]
	if "delivery_date" in df.columns:
		df["date"] = df["delivery_date"]
	
	# Normalize all dates to ISO format (YYYY-MM-DD) for consistent pivot tables
	if "date" in df.columns:
		from analysis import _normalize_date_to_iso
		# Convert all dates to ISO format
		def normalize_date(date_val):
			if pd.isna(date_val) or date_val == "":
				return ""
			date_str = str(date_val).strip()
			if not date_str:
				return ""
			# Normalize any date format to ISO format (YYYY-MM-DD)
			return _normalize_date_to_iso(date_str)
		
		df["date"] = df["date"].apply(normalize_date)
	
	# Ensure source column exists for filtering
	if "source" not in df.columns:
		df["source"] = ""
	
	# Priority: Excel data (email-xlsx) over email-text
	# If Excel data exists for a material, remove ALL email-text entries for that material (Excel is authoritative)
	if "source" in df.columns and len(df) > 0:
		excel_rows = df[df["source"].str.contains("email-xlsx|^xlsx$", case=False, na=False)]
		if len(excel_rows) > 0:
			# Create set of materials that exist in Excel data
			excel_materials = set(excel_rows["id"].astype(str).unique())
			# Remove ALL email-text rows for materials that exist in Excel
			email_text_rows = df[df["source"].str.contains("email-text", case=False, na=False)]
			conflicts = []
			for idx, row in email_text_rows.iterrows():
				material = str(row["id"])
				if material in excel_materials:
					conflicts.append(idx)
			if conflicts:
				df = df.drop(conflicts)
				print(f"Removed {len(conflicts)} email-text row(s) for materials that exist in Excel data (Excel is authoritative)")
	
	# Remove duplicates based on customer, material, date, and quantity
	# Prioritize Excel data (email-xlsx) over email-text
	before_dedup = len(df)
	# Sort by source priority: email-xlsx first, then others
	if "source" in df.columns and len(df) > 0:
		df["_priority"] = df["source"].apply(lambda x: 0 if "email-xlsx" in str(x) or str(x) == "xlsx" else 1)
		df = df.sort_values("_priority")
		df = df.drop(columns=["_priority"])
	df = df.drop_duplicates(subset=["customer_name", "id", "date", "quantity"], keep="first")
	after_dedup = len(df)
	if before_dedup > after_dedup:
		print(f"Removed {before_dedup - after_dedup} duplicate row(s)")
	
	# Required columns as per user requirements (excluding source, source_file, row_index)
	df_columns = [
		"customer_name",
		"id",
		"quantity",
		"date",
	]
	
	# Keep additional columns for reference if they exist (excluding source, source_file, row_index)
	additional_cols = ["description", "unit", "urgency", "notes"]
	for col in additional_cols:
		if col in df.columns:
			df_columns.append(col)
	
	# Ensure all required columns exist
	for col in df_columns:
		if col not in df.columns:
			df[col] = ""
	
	# Remove source column from final output (it was only needed for filtering)
	if "source" in df.columns:
		df = df.drop(columns=["source"])
	
	df = df[df_columns]
	
	print("\nExtracted rows (to be written):")
	try:
		print(df.to_string(index=False))
	except Exception:
		print(df.head().to_string(index=False))
	df.to_excel(out, index=False)
	print(f"Wrote {len(df)} rows -> {out}")


def handle_xlsx_file(path: str, bedrock, dry_run: bool) -> List[Dict[str, Any]]:
	records = read_xlsx_file(path)
	if not records:
		return []

	if dry_run:
		text_blob = "\n".join([f"row={idx} " + ", ".join([f"{k}={v}" for k, v in rec.items() if v == v]) for idx, rec in enumerate(records)])
		return [{
			"customer": "",
			"material": "",
			"quantity": "",
			"unit": "",
			"delivery_date": "",
			"urgency": "",
			"notes": text_blob[:5000],
			"source": "xlsx",
			"source_file": os.path.basename(path),
			"row_index": None,
		}]

	# Direct extraction: programmatically process month columns to avoid LLM date mapping errors
	all_requirements = []
	for idx, rec in enumerate(records):
		requirements = _extract_requirements_from_excel_row(rec, bedrock, "", "", "xlsx", os.path.basename(path), idx, debug=False)
		all_requirements.extend(requirements)
	
	return all_requirements


# Removed handle_eml - .eml file handling no longer supported


def handle_eml_bytes(raw_bytes: bytes, label: str, bedrock, dry_run: bool, debug: bool = False) -> List[Dict[str, Any]]:
	parsed = parse_eml_bytes(raw_bytes, source_label=f"imap:{label}")
	
	# Filter: Only process emails sent to TARGET_EMAIL
	if not should_process_email(parsed):
		if debug:
			print(f"EMAIL IMAP: SKIPPED - not sent to {TARGET_EMAIL}. Recipients: {parsed.recipients}")
		return []
	
	# Extract customer name from email (subject/body) - priority over sender
	email_customer = _extract_customer_from_email(parsed)
	if not email_customer:
		email_customer = parsed.sender  # Fallback to sender if not found
	
	rows: List[Dict[str, Any]] = []
	if debug:
		print(f"EMAIL IMAP: subject={parsed.subject} from={parsed.sender} recipients={parsed.recipients} images={len(parsed.images)} xlsx={len(parsed.xlsx_attachments)}")
		if email_customer != parsed.sender:
			print(f"[DEBUG] Extracted customer from email: {email_customer} (instead of sender: {parsed.sender})")

	meta_header = f"Subject: {parsed.subject}\nFrom: {parsed.sender}\nDate: {parsed.date}\n"
	full_text = "\n\n".join([meta_header, parsed.plain_text, parsed.html_text]).strip()

	if full_text:
		if dry_run:
			rows.append({
				"customer": email_customer,
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": full_text[:5000],
				"source": "email-text",
				"source_file": f"{label}",
				"row_index": None,
			})
		else:
			text_rows = analyze_text_requirements(
				bedrock,
				user_text=full_text,
				system_text=(
					"Extract requirements from an email. Capture customer name (if found in the row data), material ID (product ID/code/SKU), quantity, unit, delivery date, description (product/item description if available), and notes. "
					"CRITICAL: Do NOT extract generic phrases like 'Material Requirements', 'Customer Material Requirements', 'Requirements', 'Material', 'Excel attachment', 'Table', 'Table as image', 'Format', 'Image', 'Attachment', 'Test', 'Welcome', 'Hey' as customer names. "
					"Only extract actual company/customer names that look like real business names (e.g., 'ABC Pvt Ltd', 'Cipla', 'ENCUBE ETHICALS PVT LTD', 'GSK', 'John Doe Company'). "
					"A valid customer name should: (1) contain letters, (2) look like a company/person name (not a generic word), (3) may contain company indicators like 'Pvt Ltd', 'Inc', 'Corp', etc. "
					"If you cannot find a valid customer/company name that looks real, leave the customer field empty (it will be filled from email sender). "
					"Do NOT extract header rows or rows that are just customer names without material/quantity data."
				),
				source="email-text",
				source_file=f"{label}",
				debug=debug,
			)
			rows.extend(_sanitize_rows(text_rows, email_customer, "email-text", f"{label}"))

	for (ext, img_bytes) in parsed.images:
		fmt = ext.lower()
		if fmt == "jpg":
			fmt = "jpeg"
		if dry_run:
			rows.append({
				"customer": email_customer,
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": f"[image:{fmt}]",
				"source": "email-image",
				"source_file": f"{label}",
				"row_index": None,
			})
		else:
			grid = analyze_image_table_grid(
				bedrock,
				image_bytes=img_bytes,
				image_format=fmt,
				debug=debug,
				context_text=f"From: {parsed.sender}\nSubject: {parsed.subject}\nDate: {parsed.date}"
			)
			if grid:
				if debug:
					print("[DEBUG] Grid extracted successfully, using grid expansion")
				if debug:
					print("[DEBUG] Running grid month correction...")
				grid = correct_grid_months(bedrock, grid, debug=debug)
				image_rows = expand_grid_to_requirements(grid, "email-image", f"{label}", email_customer, debug=debug)
				if debug:
					print(f"[DEBUG] Grid expansion returned {len(image_rows)} rows")
			else:
				if debug:
					print("[DEBUG] Grid extraction failed, falling back to raw image extraction")
				image_rows = analyze_image_requirements(
					bedrock,
					image_bytes=img_bytes,
					image_format=fmt,
					source="email-image",
					source_file=f"{label}",
					debug=debug,
					context_text=f"From: {parsed.sender}\nSubject: {parsed.subject}\nDate: {parsed.date}"
				)
				if debug:
					print(f"[DEBUG] Raw image extraction returned {len(image_rows)} rows")
			rows.extend(_sanitize_rows(image_rows, email_customer, "email-image", f"{label}"))

	for (filename, xbytes) in parsed.xlsx_attachments:
		try:
			records = read_xlsx_bytes(xbytes)
		except Exception:
			records = []
		if not records:
			continue
		if dry_run:
			text_blob = "\n".join([f"row={idx} " + ", ".join([f"{k}={v}" for k, v in rec.items() if v == v]) for idx, rec in enumerate(records)])
			rows.append({
				"customer": email_customer,
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": text_blob[:5000],
				"source": "email-xlsx",
				"source_file": filename or f"{label}",
				"row_index": None,
			})
		else:
			# Direct extraction: programmatically process month columns
			# Track customer name across rows (for Excel files where customer name is in a different row)
			last_customer = email_customer or parsed.sender or ""
			if debug:
				print(f"[DEBUG] Processing {len(records)} Excel rows from {filename or f'{label}'}")
				if records:
					# Show first record's column names to debug header detection
					first_rec = records[0]
					import re as _re
					month_pattern = _re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-zA-Z\-']*\s?\d{2,4}$", _re.IGNORECASE)
					all_cols = list(first_rec.keys())
					month_cols = [c for c in all_cols if month_pattern.match(str(c).strip())]
					print(f"[DEBUG] First record columns: {all_cols[:10]}... (total: {len(all_cols)})")
					print(f"[DEBUG] Month columns found: {month_cols[:10]}... (total: {len(month_cols)})")
			for idx, rec in enumerate(records):
				# Check if this row has a customer name (but might not have material code)
				customer_candidates = ["Customer", "Customer Name", "Company"]
				row_customer = ""
				for col_name in customer_candidates:
					for k, v in rec.items():
						if col_name.lower() in str(k).lower():
							row_customer = str(v).strip() if v == v else ""
							if row_customer:
								last_customer = row_customer  # Update last seen customer
								break
					if row_customer:
						break
				# Use last_customer if row doesn't have customer name
				current_customer = row_customer or last_customer or email_customer or parsed.sender or ""
				requirements = _extract_requirements_from_excel_row(rec, bedrock, parsed.sender, current_customer, "email-xlsx", filename or f"{label}", idx, debug=debug)
				if debug and requirements:
					print(f"[DEBUG] Row {idx}: extracted {len(requirements)} requirement(s)")
				rows.extend(requirements)
			if debug:
				excel_rows = [r for r in rows if r.get('source') == 'email-xlsx']
				print(f"[DEBUG] Total Excel requirements extracted from {filename or f'{label}'}: {len(excel_rows)}")
				if excel_rows:
					print(f"[DEBUG] Final captured requirements from Excel:")
					for idx, req in enumerate(excel_rows[:10]):  # Print first 10
						print(f"  [{idx}] material={req.get('material')}, quantity={req.get('quantity')}, unit={req.get('unit')}, delivery_date={req.get('delivery_date')}, customer={req.get('customer')}")
					if len(excel_rows) > 10:
						print(f"  ... and {len(excel_rows) - 10} more")

	return rows


if __name__ == "__main__":
	main()
