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
from main import handle_eml_bytes, should_process_email, _sanitize_rows, TARGET_EMAIL

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
PROCESSING_STATUS = {"status": "idle", "message": "", "rows_count": 0, "last_updated": None}

def process_emails_async():
    """Run email processing in background thread"""
    global PROCESSING_STATUS
    try:
        PROCESSING_STATUS["status"] = "processing"
        PROCESSING_STATUS["message"] = "Starting email processing..."
        PROCESSING_STATUS["last_updated"] = datetime.now().isoformat()
        
        # Capture stdout/stderr to avoid interfering with Flask
        stdout_capture = StringIO()
        stderr_capture = StringIO()
        
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            bedrock = get_bedrock_client("us-east-1")
            all_rows = []
            
            # Fetch emails
            print(f"IMAP: connecting to {IMAP_CONFIG['host']} mailbox={IMAP_CONFIG['mailbox']} criteria=\"{IMAP_CONFIG['criteria']}\" limit={IMAP_CONFIG['limit']}")
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
                PROCESSING_STATUS["status"] = "completed"
                PROCESSING_STATUS["message"] = "No requirements extracted from emails."
                PROCESSING_STATUS["rows_count"] = 0
                PROCESSING_STATUS["last_updated"] = datetime.now().isoformat()
                return
            
            # Create DataFrame and save
            import pandas as pd
            df = pd.DataFrame(all_rows)
            
            # Map fields to match requirements
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
            
            df.to_excel(OUTPUT_FILE, index=False)
            
            row_count = len(df)
            PROCESSING_STATUS["status"] = "completed"
            PROCESSING_STATUS["message"] = f"Processing completed successfully. Extracted {row_count} requirement rows."
            PROCESSING_STATUS["rows_count"] = row_count
            
    except Exception as e:
        import traceback
        PROCESSING_STATUS["status"] = "error"
        PROCESSING_STATUS["message"] = f"Error during processing: {str(e)}\n{traceback.format_exc()}"
    
    PROCESSING_STATUS["last_updated"] = datetime.now().isoformat()

@app.route('/')
def index():
    """Main page with trigger button"""
    return render_template('index.html')

@app.route('/api/process', methods=['POST'])
def trigger_process():
    """Trigger email processing"""
    global PROCESSING_STATUS
    
    if PROCESSING_STATUS["status"] == "processing":
        return jsonify({"error": "Processing already in progress"}), 400
    
    # Start processing in background thread
    thread = threading.Thread(target=process_emails_async)
    thread.daemon = True
    thread.start()
    
    return jsonify({"message": "Processing started"})

@app.route('/api/status')
def get_status():
    """Get processing status"""
    return jsonify(PROCESSING_STATUS)

@app.route('/api/data')
def get_data():
    """Get processed data as JSON"""
    if not os.path.exists(OUTPUT_FILE):
        return jsonify({"error": "No data file found"}), 404
    
    try:
        df = pd.read_excel(OUTPUT_FILE)
        # Convert to JSON-friendly format
        data = df.fillna("").to_dict(orient='records')
        return jsonify({
            "columns": list(df.columns),
            "data": data,
            "row_count": len(df)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

