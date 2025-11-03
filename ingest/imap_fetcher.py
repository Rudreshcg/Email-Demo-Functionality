import imaplib
from typing import List, Tuple, Optional


class ImapConnection:
	def __init__(self, host: str, username: str, password: str, port: int = 993, ssl: bool = True):
		self.host = host
		self.username = username
		self.password = password
		self.port = port
		self.ssl = ssl
		self.client: Optional[imaplib.IMAP4] = None

	def __enter__(self):
		self.client = imaplib.IMAP4_SSL(self.host, self.port) if self.ssl else imaplib.IMAP4(self.host, self.port)
		self.client.login(self.username, self.password)
		return self.client

	def __exit__(self, exc_type, exc, tb):
		try:
			if self.client is not None:
				self.client.logout()
		except Exception:
			pass


def fetch_emails(
	host: str,
	username: str,
	password: str,
	mailbox: str = "INBOX",
	criteria: str = "ALL",
	limit: int = 5,
) -> List[Tuple[bytes, str]]:
	"""Return list of (raw_email_bytes, uid) tuples matching search criteria.
	criteria examples: 'UNSEEN', 'ALL', 'SINCE 01-Oct-2025'
	Fetches the most recent N emails (limit) regardless of read status when criteria is 'ALL'.
	"""
	results: List[Tuple[bytes, str]] = []
	with ImapConnection(host, username, password) as M:
		M.select(mailbox)
		status, data = M.search(None, criteria)
		if status != 'OK' or not data or not data[0]:
			return results
		uids = data[0].split()
		# For most recent emails, take the last N UIDs (newest first after reverse)
		uids = uids[-limit:] if limit and len(uids) > limit else uids
		
		# Only check for Seen flag if specifically looking for UNSEEN emails
		check_unseen = criteria.upper() == "UNSEEN"
		
		for uid in uids:
			if check_unseen:
				# Verify email is still unread by checking flags (only when UNSEEN criteria)
				status_flags, flags_data = M.fetch(uid, '(FLAGS)')
				if status_flags == 'OK' and flags_data:
					# Parse IMAP flags response format
					try:
						flags_entry = flags_data[0]
						if isinstance(flags_entry, tuple):
							flags_str = flags_entry[0].decode('utf-8', errors='ignore') if isinstance(flags_entry[0], bytes) else str(flags_entry[0])
						else:
							flags_str = str(flags_entry)
						# Skip if email is marked as Seen (already read) when looking for UNSEEN
						if '\\Seen' in flags_str:
							continue
					except Exception:
						# If flag parsing fails, continue anyway (will rely on UNSEEN search)
						pass
			
			# Use BODY.PEEK[] to fetch email content WITHOUT marking as SEEN
			# This is crucial: BODY.PEEK[] doesn't change the email's read status
			status, msg_data = M.fetch(uid, '(BODY.PEEK[])')
			if status != 'OK' or not msg_data:
				continue
			for part in msg_data:
				if isinstance(part, tuple) and part[1]:
					results.append((part[1], uid.decode('utf-8', errors='ignore')))
	return results
