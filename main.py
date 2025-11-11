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
from parsers.xlsx_parser import read_xlsx_file
from analysis import _normalize_date_to_iso
from ingest.imap_fetcher import fetch_emails
from processors.common import dedupe_requirements, sanitize_rows
from processors import excel_processor, html_processor, image_processor, text_processor


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

	all_rows = dedupe_requirements(all_rows)

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

	filename = os.path.basename(path)

	if dry_run:
		return [
			{
				"customer": "",
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": f"[dry-run] detected {len(records)} row(s) in {filename}",
				"source": "xlsx",
				"source_file": filename,
				"row_index": None,
			}
		]

	all_requirements: List[Dict[str, Any]] = []
	for idx, rec in enumerate(records):
		raw = excel_processor.extract_requirements_from_excel_row(
			rec,
			bedrock,
			"",
			"",
			"xlsx",
			filename,
			idx,
			debug=False,
		)
		all_requirements.extend(
			sanitize_rows(
				raw,
				"",
				"xlsx",
				filename,
			)
		)

	return dedupe_requirements(all_requirements)


# Removed handle_eml - .eml file handling no longer supported


def handle_eml_bytes(raw_bytes: bytes, label: str, bedrock, dry_run: bool, debug: bool = False) -> List[Dict[str, Any]]:
    parsed = parse_eml_bytes(raw_bytes, source_label=f"imap:{label}")

    if not should_process_email(parsed):
        if debug:
            print(
                f"EMAIL IMAP: SKIPPED - not sent to {TARGET_EMAIL}. Recipients: {parsed.recipients}"
            )
        return []

    email_customer = _extract_customer_from_email(parsed)
    if not email_customer:
        email_customer = parsed.sender

    if debug:
        print(
            f"EMAIL IMAP: subject={parsed.subject} from={parsed.sender} "
            f"recipients={parsed.recipients} images={len(parsed.images)} "
            f"xlsx={len(parsed.xlsx_attachments)}"
        )
        if email_customer != parsed.sender:
            print(
                f"[DEBUG] Extracted customer from email: {email_customer} "
                f"(instead of sender: {parsed.sender})"
            )

    if dry_run:
        placeholders: List[Dict[str, Any]] = []
        for ext, _ in parsed.images or []:
            fmt = ext.lower()
            if fmt == "jpg":
                fmt = "jpeg"
            placeholders.append(
                {
                    "customer": email_customer,
                    "material": "",
                    "quantity": "",
                    "unit": "",
                    "delivery_date": "",
                    "urgency": "",
                    "notes": f"[image:{fmt}]",
                    "source": "email-image",
                    "source_file": str(label),
                    "row_index": None,
                }
            )
        for filename, _ in parsed.xlsx_attachments or []:
            placeholders.append(
                {
                    "customer": email_customer,
                    "material": "",
                    "quantity": "",
                    "unit": "",
                    "delivery_date": "",
                    "urgency": "",
                    "notes": f"[xlsx:{filename}]",
                    "source": "email-xlsx",
                    "source_file": str(label),
                    "row_index": None,
                }
            )
        return placeholders

    html_rows = html_processor.extract_html_tables(
        parsed,
        email_customer,
        str(label),
        debug=debug,
    )
    excel_rows = excel_processor.extract_excel_attachments(
        parsed,
        bedrock,
        email_customer,
        str(label),
        debug=debug,
    )
    image_rows = image_processor.extract_image_requirements(
        parsed,
        bedrock,
        email_customer,
        str(label),
        debug=debug,
    )

    rows: List[Dict[str, Any]] = []
    rows.extend(html_rows)
    rows.extend(excel_rows)
    rows.extend(image_rows)

    if debug:
        print(
            f"[DEBUG] Extracted counts -> html: {len(html_rows)}, "
            f"excel: {len(excel_rows)}, image: {len(image_rows)}"
        )

    if not rows:
        text_rows = text_processor.extract_text_requirements(
            parsed,
            bedrock,
            email_customer,
            str(label),
            debug=debug,
        )
        rows.extend(text_rows)
    elif debug:
        print(
            "[DEBUG] Structured data detected; skipping text model extraction to avoid duplicates"
        )

    return dedupe_requirements(rows)


if __name__ == "__main__":
	main()
