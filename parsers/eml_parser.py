import os
import re
import base64
from typing import Dict, List, Optional, Tuple
from email import policy
from email.parser import BytesParser
from bs4 import BeautifulSoup


class ParsedEmail:
	def __init__(self, source_path: str):
		self.source_path = source_path
		self.subject: str = ""
		self.sender: str = ""
		self.recipients: List[str] = []  # List of recipient email addresses
		self.date: str = ""
		self.plain_text: str = ""
		self.html_text: str = ""
		self.images: List[Tuple[str, bytes]] = []  # (ext, bytes)
		self.xlsx_attachments: List[Tuple[str, bytes]] = []  # (filename, bytes)


def _html_to_text(html: str) -> str:
	soup = BeautifulSoup(html, "lxml")
	for tag in soup(["script", "style"]):
		tag.extract()
	text = soup.get_text("\n")
	text = re.sub(r"\n\s*\n+", "\n\n", text)
	return text.strip()


def _parse_msg(msg, source_label: str) -> ParsedEmail:
	parsed = ParsedEmail(source_path=source_label)
	parsed.subject = msg.get("subject", "") or ""
	parsed.sender = msg.get("from", "") or ""
	
	# Extract recipient addresses (To, Cc, Bcc)
	recipients = []
	to_header = msg.get("to", "") or ""
	cc_header = msg.get("cc", "") or ""
	bcc_header = msg.get("bcc", "") or ""
	
	# Parse email addresses from headers
	from email.utils import getaddresses
	if to_header:
		recipients.extend([addr for name, addr in getaddresses([to_header]) if addr])
	if cc_header:
		recipients.extend([addr for name, addr in getaddresses([cc_header]) if addr])
	if bcc_header:
		recipients.extend([addr for name, addr in getaddresses([bcc_header]) if addr])
	
	parsed.recipients = list(set([r.lower().strip() for r in recipients if r]))  # Normalize and deduplicate
	parsed.date = msg.get("date", "") or ""

	plain_parts: List[str] = []
	html_parts: List[str] = []

	if msg.is_multipart():
		for part in msg.walk():
			content_type = part.get_content_type()
			disposition = (part.get("Content-Disposition") or "").lower()
			if content_type == "text/plain" and "attachment" not in disposition:
				try:
					plain_parts.append(part.get_content())
				except Exception:
					continue
			elif content_type == "text/html" and "attachment" not in disposition:
				try:
					html_parts.append(part.get_content())
				except Exception:
					continue
			elif content_type.startswith("image/"):
				ext = content_type.split("/")[-1]
				try:
					parsed.images.append((ext, part.get_content()))
				except Exception:
					continue
			elif (
				content_type 
				in (
					"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
					"application/vnd.ms-excel",
					"application/octet-stream",
				)
			) and (
				"xlsx" in (part.get_filename() or "").lower()
			):
				filename = part.get_filename() or "attachment.xlsx"
				try:
					parsed.xlsx_attachments.append((filename, part.get_content()))
				except Exception:
					continue
	else:
		content_type = msg.get_content_type()
		if content_type == "text/plain":
			plain_parts.append(msg.get_content())
		elif content_type == "text/html":
			html_parts.append(msg.get_content())

	parsed.plain_text = "\n\n".join(p.strip() for p in plain_parts if p).strip()
	parsed.html_text = "\n\n".join(_html_to_text(h) for h in html_parts if h).strip()
	return parsed


def parse_eml_bytes(raw_bytes: bytes, source_label: str = "imap") -> ParsedEmail:
	msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
	return _parse_msg(msg, source_label=source_label)
