# Web Interface for Email Requirements Extractor

## Setup

1. **Install Flask** (if not already installed):
   ```bash
   pip install flask
   ```

2. **Update IMAP credentials** in `web_app.py`:
   ```python
   IMAP_CONFIG = {
       "host": "imap.gmail.com",
       "user": "orderaggregationdemo@gmail.com",
       "password": "your_app_password_here",
       "mailbox": "INBOX",
       "criteria": "ALL",
       "limit": 5,
   }
   ```

## Running the Web Interface

Start the Flask web server:

```bash
python web_app.py
```

The web interface will be available at: **http://localhost:5000**

## Features

- **Process Emails Button**: Click to fetch and process the last 5 emails from your IMAP account
- **Real-time Status**: See processing status (idle, processing, completed, error)
- **Results Table**: View extracted requirements in a formatted table below the button
- **Download Excel**: Download the processed Excel file with all requirements

## Usage

1. Open your browser and go to `http://localhost:5000`
2. Click the "🚀 Process Emails" button
3. Wait for processing to complete (status will update automatically)
4. View the extracted requirements in the table below
5. Click "📥 Download Excel" to download the results file

## Notes

- Processing runs in a background thread so the web interface remains responsive
- The system only processes emails sent to `orderaggregationdemo@gmail.com`
- Processing may take 1-2 minutes depending on the number of emails and images

