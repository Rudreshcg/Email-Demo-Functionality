from flask import Flask, render_template, jsonify, request, send_file
import os
import sys
import threading
import json
from datetime import datetime
import pandas as pd
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Import processing functions directly
from bedrock_client import get_bedrock_client
from ingest.imap_fetcher import fetch_emails
from main import handle_eml_bytes, should_process_email, TARGET_EMAIL

app = Flask(__name__)

# Configuration - update these with your IMAP credentials
IMAP_CONFIG = {
    "host": "imap.gmail.com",
    "user": "orderaggregationdemo@gmail.com",
    "password": "tmkr mbvm jbze pdsv",  # Replace with environment variable in production
    "mailbox": "INBOX",
    "criteria": "ALL",
    "limit": 5,
}

# SMTP configuration for sending HTML emails
SMTP_CONFIG = {
    "host": "smtp.gmail.com",
    "port": 587,
    "user": IMAP_CONFIG["user"],
    "password": IMAP_CONFIG["password"],
}

OUTPUT_FILE = "requirements_output.xlsx"
STATUS_FILE = "processing_status.json"
STATUS_LOCK = threading.Lock()

DEFAULT_STATUS = {
    "status": "idle",
    "message": "",
    "rows_count": 0,
    "last_updated": None,
    "columns": [],
    "data": [],
}


def _status_with_defaults(data=None):
    """Merge persisted data with default structure."""
    status = DEFAULT_STATUS.copy()
    if isinstance(data, dict):
        status.update({k: v for k, v in data.items() if k in status})
    return status


def _read_status_file():
    """Read processing status from disk, returning defaults on failure."""
    if not os.path.exists(STATUS_FILE):
        return DEFAULT_STATUS.copy()
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return _status_with_defaults(data)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_STATUS.copy()


def _write_status_file(status):
    """Persist processing status to disk atomically."""
    tmp_path = f"{STATUS_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATUS_FILE)


PROCESSING_STATUS = _status_with_defaults(_read_status_file())


def _sync_status_from_disk():
    """Refresh in-memory status from disk and return a copy."""
    latest = _status_with_defaults(_read_status_file())
    with STATUS_LOCK:
        PROCESSING_STATUS.clear()
        PROCESSING_STATUS.update(latest)
        return PROCESSING_STATUS.copy()


def _set_status(**kwargs):
    """Update status atomically and persist to disk."""
    with STATUS_LOCK:
        PROCESSING_STATUS.update(kwargs)
        PROCESSING_STATUS["last_updated"] = datetime.now().isoformat()
        _write_status_file(PROCESSING_STATUS)
        return PROCESSING_STATUS.copy()


def _clear_output_file():
    """Remove the output Excel file if it exists."""
    if os.path.exists(OUTPUT_FILE):
        try:
            os.remove(OUTPUT_FILE)
        except OSError:
            pass

def process_emails_async():
    """Run email processing in background thread"""
    try:
        _set_status(
            status="processing",
            message="Starting email processing...",
            rows_count=0,
            columns=[],
            data=[],
        )

        _clear_output_file()

        # Capture stdout/stderr to avoid interfering with Flask
        stdout_capture = StringIO()
        stderr_capture = StringIO()

        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            bedrock = get_bedrock_client("us-east-1")
            all_rows = []

            # Fetch emails
            print(
                f"IMAP: connecting to {IMAP_CONFIG['host']} mailbox={IMAP_CONFIG['mailbox']} "
                f"criteria=\"{IMAP_CONFIG['criteria']}\" limit={IMAP_CONFIG['limit']}"
            )
            emails = fetch_emails(
                host=IMAP_CONFIG["host"],
                username=IMAP_CONFIG["user"],
                password=IMAP_CONFIG["password"],
                mailbox=IMAP_CONFIG["mailbox"],
                criteria=IMAP_CONFIG["criteria"],
                limit=IMAP_CONFIG["limit"],
            )
            print(f"IMAP: fetched {len(emails)} message(s)")

            for raw_bytes, uid in emails:
                rows = handle_eml_bytes(raw_bytes, uid, bedrock, dry_run=False, debug=False)
                all_rows.extend(rows)

            if not all_rows:
                _set_status(
                    status="completed",
                    message="No requirements extracted from emails.",
                    rows_count=0,
                    columns=[],
                    data=[],
                )
                _clear_output_file()
                return

            df = pd.DataFrame(all_rows)

            if "customer" in df.columns:
                df["customer_name"] = df["customer"]
            if "material" in df.columns:
                df["id"] = df["material"]
            if "delivery_date" in df.columns:
                df["date"] = df["delivery_date"]

            df_columns = ["customer_name", "id", "quantity", "date"]
            additional_cols = ["unit", "urgency", "notes", "source", "source_file", "row_index"]
            for col in additional_cols:
                if col in df.columns:
                    df_columns.append(col)

            for col in df_columns:
                if col not in df.columns:
                    df[col] = ""
            df = df[df_columns]

            df = df.fillna("")
            _clear_output_file()
            df.to_excel(OUTPUT_FILE, index=False)

            rows = df.to_dict(orient="records")
            row_count = len(rows)
            columns = df.columns.tolist()

            _set_status(
                columns=columns,
                data=rows,
                rows_count=row_count,
                status="completed",
                message=f"Processing completed successfully. Extracted {row_count} requirement rows.",
            )

    except Exception as exc:  # noqa: BLE001
        import traceback

        _set_status(
            status="error",
            message=f"Error during processing: {exc}\n{traceback.format_exc()}",
            rows_count=0,
            columns=[],
            data=[],
        )


@app.after_request
def add_no_cache_headers(response):
    """Prevent browsers from caching API responses so fresh data is always fetched."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route('/')
def index():
    """Main page with trigger button"""
    return render_template('index.html')

@app.route('/api/process', methods=['POST'])
def trigger_process():
    """Trigger email processing"""
    current_status = _sync_status_from_disk()

    if current_status.get("status") == "processing":
        return jsonify({"error": "Processing already in progress"}), 400
    
    _set_status(
        status="processing",
        message="Starting email processing...",
        rows_count=0,
        columns=[],
        data=[],
    )
    _clear_output_file()
    
    # Start processing in background thread
    thread = threading.Thread(target=process_emails_async)
    thread.daemon = True
    thread.start()
    
    return jsonify({"message": "Processing started"})

@app.route('/api/status')
def get_status():
    """Get processing status"""
    status = _sync_status_from_disk()
    return jsonify(status)

@app.route('/api/data')
def get_data():
    """Get processed data as JSON"""
    status = _sync_status_from_disk()
    columns = status.get("columns", [])
    rows = status.get("data", [])
    row_count = status.get("rows_count", len(rows))
    return jsonify({"columns": columns, "data": rows, "row_count": row_count})

@app.route('/api/download')
def download_file():
    """Download the Excel file"""
    if not os.path.exists(OUTPUT_FILE):
        return jsonify({"error": "File not found"}), 404
    return send_file(OUTPUT_FILE, as_attachment=True, download_name="requirements_output.xlsx")


def _wrap_html_document(inner_html: str) -> str:
    """Wrap HTML body in a minimal document to improve Outlook/Gmail rendering."""
    safe_html = (inner_html or "").replace("’", "&rsquo;")
    return (
        "<!DOCTYPE html>\n"
        "<html>\n<head>\n<meta http-equiv=\"Content-Type\" content=\"text/html; charset=utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "</head>\n<body style=\"margin:0;padding:0;\">\n"
        f"{safe_html}\n"
        "</body>\n</html>"
    )


def send_email_with_table(to_email: str, subject: str, html_body: str):
    """Send a standards-compliant HTML email (multipart/alternative)."""
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = SMTP_CONFIG["user"]
        msg['To'] = to_email
        msg['Subject'] = subject

        # Plain text fallback
        fallback = "Material Requirements table included in HTML body."
        msg.attach(MIMEText(fallback, 'plain', 'utf-8'))

        html_doc = _wrap_html_document(html_body)
        msg.attach(MIMEText(html_doc, 'html', 'utf-8'))

        server = smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"])
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_CONFIG["user"], SMTP_CONFIG["password"])
        server.send_message(msg)
        server.quit()
        return True, "Email sent successfully"
    except Exception as e:
        return False, str(e)


@app.route('/api/send-raw-html', methods=['POST'])
def send_raw_html():
    """Send the provided HTML snippet as an email body (for Outlook/Gmail)."""
    try:
        data = request.json or {}
        to_email = data.get("to", IMAP_CONFIG["user"])  # default to account address
        subject = data.get("subject", "Material Requirements - LL (Export market)")
        html = data.get("html", "")
        if not html:
            return jsonify({"error": "Missing 'html' in request body"}), 400
        ok, msg = send_email_with_table(to_email, subject, html)
        if ok:
            return jsonify({"message": msg, "to": to_email})
        return jsonify({"error": msg}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    os.makedirs('templates', exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)

