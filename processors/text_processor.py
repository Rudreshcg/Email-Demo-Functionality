from typing import Dict, List

from analysis import analyze_text_requirements

from .common import sanitize_rows


def extract_text_requirements(
    parsed_email,
    bedrock,
    email_customer: str,
    label: str,
    debug: bool = False,
) -> List[Dict[str, str]]:
    meta_header = (
        f"Subject: {parsed_email.subject}\n"
        f"From: {parsed_email.sender}\n"
        f"Date: {parsed_email.date}\n"
    )
    full_text = "\n\n".join(
        [
            meta_header,
            parsed_email.plain_text or "",
            parsed_email.html_text or "",
        ]
    ).strip()
    if not full_text:
        return []
    text_rows = analyze_text_requirements(
        bedrock,
        user_text=full_text,
        system_text=None,
        source="email-text",
        source_file=str(label),
        debug=debug,
    )
    return sanitize_rows(
        text_rows,
        email_customer,
        "email-text",
        str(label),
    )

