import os
import io
import json
import mimetypes
from typing import List, Dict, Any

import click
import pandas as pd

from bedrock_client import get_bedrock_client
from parsers.eml_parser import parse_eml_file, parse_eml_bytes
from parsers.xlsx_parser import read_xlsx_file, read_xlsx_bytes
from analysis import analyze_text_requirements, analyze_image_requirements, analyze_image_table_grid, expand_grid_to_requirements, correct_grid_months, refine_projection_with_image
from ingest.imap_fetcher import fetch_emails


SUPPORTED_XLSX_EXTS = {".xlsx"}
SUPPORTED_EML_EXTS = {".eml"}

# Target email address for order aggregation
TARGET_EMAIL = "orderaggregationdemo@gmail.com"


def is_xlsx_file(path: str) -> bool:
	return os.path.splitext(path)[1].lower() in SUPPORTED_XLSX_EXTS


def is_eml_file(path: str) -> bool:
	return os.path.splitext(path)[1].lower() in SUPPORTED_EML_EXTS


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


def _sanitize_rows(rows: List[Dict[str, Any]], customer: str, source: str, source_file: str) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    for item in rows or []:
        qty = _coerce_number(item.get("quantity", 0))
        # drop zero or negative quantities
        if qty <= 0:
            continue
        notes = str(item.get("notes", "") or "")
        # skip dropped SKUs
        if "dropped" in notes.lower():
            continue
        out = dict(item)
        out["quantity"] = qty
        out["customer"] = customer or out.get("customer", "")
        out["source"] = source
        out["source_file"] = source_file
        cleaned.append(out)
    return cleaned


@click.command()
@click.option("--input-dir", required=False, default=None, type=click.Path(exists=True, file_okay=False), help="Folder containing .eml and .xlsx files")
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
			if is_eml_file(path):
				print(f"FILES: parsing email {os.path.basename(path)}")
				rows = handle_eml(path, bedrock, dry_run, debug)
				print(f"FILES: {os.path.basename(path)} -> extracted {len(rows)} row(s)")
				all_rows.extend(rows)
				processed += 1
			elif is_xlsx_file(path):
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
	
	# Required columns as per user requirements
	df_columns = [
		"customer_name",
		"id",
		"quantity",
		"date",
	]
	
	# Keep additional columns for reference if they exist
	additional_cols = ["unit", "urgency", "notes", "source", "source_file", "row_index"]
	for col in additional_cols:
		if col in df.columns:
			df_columns.append(col)
	
	# Ensure all required columns exist
	for col in df_columns:
		if col not in df.columns:
			df[col] = ""
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

	# Convert records to text for LLM extraction
	lines: List[str] = []
	for idx, rec in enumerate(records):
		pairs = [f"{k}={v}" for k, v in rec.items() if v == v]  # filter NaN
		lines.append(f"row={idx} " + ", ".join(pairs))
	text_blob = "\n".join(lines)

	if dry_run:
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

	rows = 				analyze_text_requirements(
					bedrock,
					user_text=text_blob,
					system_text=(
						"You convert tabular order data into structured requirements. "
						"Extract customer name, material ID (product ID/code/SKU), quantity, unit, and delivery date. "
						"Use row context if it clarifies quantities or dates."
					),
					source="xlsx",
					source_file=os.path.basename(path),
				)
	return rows


def handle_eml(path: str, bedrock, dry_run: bool, debug: bool = False) -> List[Dict[str, Any]]:
	parsed = parse_eml_file(path)
	
	# Filter: Only process emails sent to TARGET_EMAIL
	if not should_process_email(parsed):
		if debug:
			print(f"EMAIL FILE: SKIPPED - not sent to {TARGET_EMAIL}. Recipients: {parsed.recipients}")
		return []
	
	rows: List[Dict[str, Any]] = []
	if debug:
		print(f"EMAIL FILE: subject={parsed.subject} from={parsed.sender} recipients={parsed.recipients} images={len(parsed.images)} xlsx={len(parsed.xlsx_attachments)}")

	meta_header = f"Subject: {parsed.subject}\nFrom: {parsed.sender}\nDate: {parsed.date}\n"
	full_text = "\n\n".join([meta_header, parsed.plain_text, parsed.html_text]).strip()

	if full_text:
		if dry_run:
			rows.append({
				"customer": parsed.sender,
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": full_text[:5000],
				"source": "email-text",
				"source_file": os.path.basename(path),
				"row_index": None,
			})
		else:
			rows.extend(
				analyze_text_requirements(
					bedrock,
					user_text=full_text,
					system_text=(
						"Extract requirements from an email. Capture customer name, material ID (product ID/code/SKU), quantity, unit, delivery date, and notes."
					),
					source="email-text",
					source_file=os.path.basename(path),
					debug=debug,
				)
			)

	# Process inline and attached images
	for (ext, img_bytes) in parsed.images:
		fmt = ext.lower()
		if fmt == "jpg":
			fmt = "jpeg"
			
		if dry_run:
			rows.append({
				"customer": parsed.sender,
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": f"[image:{fmt}]",
				"source": "email-image",
				"source_file": os.path.basename(path),
				"row_index": None,
			})
		else:
			# Try grid-first extraction for higher accuracy
			grid = analyze_image_table_grid(
				bedrock,
				image_bytes=img_bytes,
				image_format=fmt,
				debug=debug,
				context_text=f"From: {parsed.sender}\nSubject: {parsed.subject}\nDate: {parsed.date}"
			)
			if grid:
				if debug:
					print("[DEBUG] Running grid month correction...")
				grid = correct_grid_months(bedrock, grid, debug=debug)
				# Second pass: validate Jul/Aug using the image itself (temporarily disabled to rely on heuristic)
				# if debug:
				#     print("[DEBUG] Refining projection with image...")
				# grid = refine_projection_with_image(bedrock, img_bytes, fmt, grid, debug=debug)
				image_rows = expand_grid_to_requirements(grid, "email-image", os.path.basename(path), parsed.sender, debug=debug)
			else:
				image_rows = analyze_image_requirements(
				bedrock,
				image_bytes=img_bytes,
				image_format=fmt,
				source="email-image",
				source_file=os.path.basename(path),
				debug=debug,
				context_text=f"From: {parsed.sender}\nSubject: {parsed.subject}\nDate: {parsed.date}"
				)
			rows.extend(_sanitize_rows(image_rows, parsed.sender, "email-image", os.path.basename(path)))

	# Process xlsx attachments
	for (filename, xbytes) in parsed.xlsx_attachments:
		try:
			records = read_xlsx_bytes(xbytes)
		except Exception:
			records = []
		if not records:
			continue
		lines: List[str] = []
		for idx, rec in enumerate(records):
			pairs = [f"{k}={v}" for k, v in rec.items() if v == v]
			lines.append(f"row={idx} " + ", ".join(pairs))
		text_blob = "\n".join(lines)

		if dry_run:
			rows.append({
				"customer": parsed.sender,
				"material": "",
				"quantity": "",
				"unit": "",
				"delivery_date": "",
				"urgency": "",
				"notes": text_blob[:5000],
				"source": "email-xlsx",
				"source_file": os.path.basename(filename or os.path.basename(path)),
				"row_index": None,
			})
		else:
			rows.extend(
				analyze_text_requirements(
					bedrock,
					user_text=text_blob,
					system_text=(
						"You convert tabular order data into structured requirements. "
						"Extract customer name, material ID (product ID/code/SKU), quantity, unit, and delivery date. "
						"Use row context if it clarifies quantities or dates."
					),
					source="email-xlsx",
					source_file=filename or os.path.basename(path),
					debug=debug,
				)
			)

	return rows


def handle_eml_bytes(raw_bytes: bytes, label: str, bedrock, dry_run: bool, debug: bool = False) -> List[Dict[str, Any]]:
	parsed = parse_eml_bytes(raw_bytes, source_label=f"imap:{label}")
	
	# Filter: Only process emails sent to TARGET_EMAIL
	if not should_process_email(parsed):
		if debug:
			print(f"EMAIL IMAP: SKIPPED - not sent to {TARGET_EMAIL}. Recipients: {parsed.recipients}")
		return []
	
	rows: List[Dict[str, Any]] = []
	if debug:
		print(f"EMAIL IMAP: subject={parsed.subject} from={parsed.sender} recipients={parsed.recipients} images={len(parsed.images)} xlsx={len(parsed.xlsx_attachments)}")

	meta_header = f"Subject: {parsed.subject}\nFrom: {parsed.sender}\nDate: {parsed.date}\n"
	full_text = "\n\n".join([meta_header, parsed.plain_text, parsed.html_text]).strip()

	if full_text:
		if dry_run:
			rows.append({
				"customer": parsed.sender,
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
			rows.extend(
				analyze_text_requirements(
					bedrock,
					user_text=full_text,
					system_text=(
						"Extract requirements from an email. Capture customer name, material ID (product ID/code/SKU), quantity, unit, delivery date, and notes."
					),
					source="email-text",
					source_file=f"{label}",
					debug=debug,
				)
			)

	for (ext, img_bytes) in parsed.images:
		fmt = ext.lower()
		if fmt == "jpg":
			fmt = "jpeg"
		if dry_run:
			rows.append({
				"customer": parsed.sender,
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
					print("[DEBUG] Running grid month correction...")
				grid = correct_grid_months(bedrock, grid, debug=debug)
				# if debug:
				#     print("[DEBUG] Refining projection with image...")
				# grid = refine_projection_with_image(bedrock, img_bytes, fmt, grid, debug=debug)
				image_rows = expand_grid_to_requirements(grid, "email-image", f"{label}", parsed.sender, debug=debug)
			else:
				image_rows = analyze_image_requirements(
				bedrock,
				image_bytes=img_bytes,
				image_format=fmt,
				source="email-image",
				source_file=f"{label}",
				debug=debug,
				context_text=f"From: {parsed.sender}\nSubject: {parsed.subject}\nDate: {parsed.date}"
				)
			rows.extend(_sanitize_rows(image_rows, parsed.sender, "email-image", f"{label}"))

	for (filename, xbytes) in parsed.xlsx_attachments:
		try:
			records = read_xlsx_bytes(xbytes)
		except Exception:
			records = []
		if not records:
			continue
		lines: List[str] = []
		for idx, rec in enumerate(records):
			pairs = [f"{k}={v}" for k, v in rec.items() if v == v]
			lines.append(f"row={idx} " + ", ".join(pairs))
		text_blob = "\n".join(lines)

		if dry_run:
			rows.append({
				"customer": parsed.sender,
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
			rows.extend(
				analyze_text_requirements(
					bedrock,
					user_text=text_blob,
					system_text=(
						"You convert tabular order data into structured requirements. "
						"Extract customer name, material ID (product ID/code/SKU), quantity, unit, and delivery date. "
						"Use row context if it clarifies quantities or dates."
					),
					source="email-xlsx",
					source_file=filename or f"{label}",
					debug=debug,
				)
			)

	return rows


if __name__ == "__main__":
	main()
