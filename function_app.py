import azure.functions as func
import logging
import os
import json
import uuid
import pyodbc
import requests
import re
import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote, quote
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
)

app = func.FunctionApp()

_GHL_FIELD_MAP_CACHE = None

GHL_CLIENT_SUBMISSION_TAG = os.getenv(
    "GHL_CLIENT_SUBMISSION_TAG",
    "client-submission-received",
).strip() or "client-submission-received"

GHL_CLIENT_SUBMISSION_WORKFLOW_ID = os.getenv(
    "GHL_CLIENT_SUBMISSION_WORKFLOW_ID",
    "",
).strip()

GHL_PASSWORD_CHANGED_TAG = os.getenv(
    "GHL_PASSWORD_CHANGED_TAG",
    "client-password-changed",
).strip() or "client-password-changed"

PASSWORD_HASH_ITERATIONS = int(
    os.getenv("PASSWORD_HASH_ITERATIONS", "310000")
)

TRANSACTION_DOCUMENTS = {
    "alt_doc": [
        "bas-from-ato-portal",
        "business-banking-statements",
        "id",
        "passport",
        "last-6-months-mortgage-statements",
        "council-rates-notice",
    ],
    "full_doc": [
        "payslip",
        "management-reports-financial-statements",
        "group-certificate-payment-summary",
        "id",
        "passport",
        "company-tax-returns",
        "individual-tax-returns",
        "last-6-months-mortgage-statements",
        "council-rates-notice",
    ],
}

DOCUMENT_LABELS = {
    "bas-from-ato-portal": "BAS from ATO Portal",
    "business-banking-statements": "Business Banking Statements",
    "id": "ID",
    "passport": "Passport",
    "payslip": "Payslip",
    "management-reports-financial-statements": "Management Reports / Financial Statements",
    "group-certificate-payment-summary": "Group Certificate / Payment Summary",
    "company-tax-returns": "Company Tax Returns",
    "individual-tax-returns": "Individual Tax Returns",
    "last-6-months-mortgage-statements": "Last 6 Months Mortgage Statements",
    "council-rates-notice": "Council Rates Notice",
}

DOCUMENT_INTELLIGENCE_ENDPOINT = os.getenv(
    "DOCUMENT_INTELLIGENCE_ENDPOINT",
    "",
).strip().rstrip("/")
DOCUMENT_INTELLIGENCE_KEY = os.getenv(
    "DOCUMENT_INTELLIGENCE_KEY",
    "",
).strip()
DOCUMENT_INTELLIGENCE_API_VERSION = os.getenv(
    "DOCUMENT_INTELLIGENCE_API_VERSION",
    "2024-11-30",
).strip() or "2024-11-30"
DOCUMENT_INTELLIGENCE_CLASSIFIER_ID = os.getenv(
    "DOCUMENT_INTELLIGENCE_CLASSIFIER_ID",
    "",
).strip()
DOCUMENT_COMPARISON_TIMEOUT_SECONDS = max(
    30,
    int(os.getenv("DOCUMENT_COMPARISON_TIMEOUT_SECONDS", "120")),
)

DOCUMENT_TYPE_KEYWORDS = {
    "payslip": [
        "payslip", "pay slip", "pay period", "gross pay", "net pay",
        "earnings", "deductions", "employer", "employee",
    ],
    "id": [
        "driver licence", "driver license", "identity", "date of birth",
        "licence number", "license number", "expiry", "address",
    ],
    "passport": [
        "passport", "nationality", "date of birth", "date of expiry",
        "place of birth", "passport number", "issuing authority",
    ],
    "bas-from-ato-portal": [
        "business activity statement", "australian taxation office", "ato",
        "gst", "pay as you go", "activity statement",
    ],
    "business-banking-statements": [
        "bank statement", "account number", "opening balance",
        "closing balance", "debit", "credit", "transaction",
    ],
    "management-reports-financial-statements": [
        "financial statements", "balance sheet", "profit and loss",
        "income statement", "assets", "liabilities", "equity",
    ],
    "group-certificate-payment-summary": [
        "payment summary", "income statement", "gross payments",
        "tax withheld", "payer", "payee", "financial year",
    ],
    "company-tax-returns": [
        "company tax return", "taxable income", "income tax",
        "australian taxation office", "abn", "financial year",
    ],
    "individual-tax-returns": [
        "individual tax return", "taxable income", "income tax",
        "australian taxation office", "tax file number", "financial year",
    ],
    "last-6-months-mortgage-statements": [
        "mortgage statement", "home loan", "loan account", "interest rate",
        "repayment", "principal", "outstanding balance",
    ],
    "council-rates-notice": [
        "rates notice", "council", "property", "valuation", "rateable",
        "assessment number", "amount due",
    ],
}

DOCUMENT_COMPARISON_STOP_WORDS = {
    "and", "are", "for", "from", "has", "have", "into", "not", "of",
    "on", "or", "the", "this", "to", "was", "were", "will", "with",
}


GHL_CUSTOM_FIELD_CONFIG = {
    "GHL_CUSTOM_FIELD_CLIENT_ID": ["client_id", "unique_id"],
    "GHL_CUSTOM_FIELD_DOCUMENT_STATUS": ["document_status"],
    "GHL_CUSTOM_FIELD_MISSING_DOCUMENTS": ["missing_documents"],
    "GHL_CUSTOM_FIELD_REQUIRED_DOCUMENTS": [
        "required_documents",
        "required_document_list",
        "documents_required",
    ],
    "GHL_APPLICATION_SOURCE_FIELD": ["application_source"],
    "GHL_CLASSIFICATION_FIELD": ["classification_type"],
    "GHL_BORROWER_FIELD": ["borrower_type"],
    "GHL_OBJECTIVE_FIELD": ["objective"],
    "GHL_LOAN_TYPE_FIELD": ["loan_type"],
    "GHL_PURPOSE_FIELD": ["purpose"],
    "GHL_TRANSACTION_FIELD": ["transaction_type"],
    "GHL_WITH_BORROWERS_GUARANTORS_FIELD": [
        "with_borrowers__guarantors",
        "with_borrowers_guarantors",
        "with_borrowers_guarantors?",
    ],
    "GHL_SETTLEMENT_FIELD": [
        "anticipated_settlement_date",
        "settlement_date",
    ],
    "GHL_REFERRER_FIRST_NAME_FIELD": ["referrer_first_name"],
    "GHL_REFERRER_MIDDLE_NAME_FIELD": ["referrer_middle_name"],
    "GHL_REFERRER_LAST_NAME_FIELD": ["referrer_last_name"],
    "GHL_REFERRER_PHONE_FIELD": ["referrer_phone", "referrer_mobile"],
    "GHL_REFERRER_EMAIL_FIELD": ["referrer_email"],
}

REQUIRED_INTAKE_FIELDS = {
    "email": "Email",
    "phone": "Phone number",
    "source": "Source",
    "classificationType": "Classification type",
    "borrowerType": "Borrower type",
    "objective": "Objective",
    "loanType": "Loan Type",
    "purpose": "Purpose",
    "transactionType": "Transaction type",
    "anticipatedSettlementDate": "Anticipated Settlement Date",
}

LEAD_TYPE_LABELS = {
    "business_owner": "Business Owner",
    "business-owner": "Business Owner",
    "business owner": "Business Owner",
    "broker": "Broker",
    "referral": "Referral",
    "direct-client": "Direct Client",
    "direct client": "Direct Client",
    "referrer": "Referrer",
}


def add_cors(response: func.HttpResponse) -> func.HttpResponse:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


def get_sql_connection():
    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={os.getenv('SQL_SERVER')};"
        f"DATABASE={os.getenv('SQL_DATABASE')};"
        f"UID={os.getenv('SQL_USERNAME')};"
        f"PWD={os.getenv('SQL_PASSWORD')};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str)


def clean_value(value):
    return (value or "").strip()


def document_intelligence_is_configured() -> bool:
    return bool(DOCUMENT_INTELLIGENCE_ENDPOINT and DOCUMENT_INTELLIGENCE_KEY)


def document_intelligence_headers(content_type=None):
    headers = {
        "Ocp-Apim-Subscription-Key": DOCUMENT_INTELLIGENCE_KEY,
    }

    if content_type:
        headers["Content-Type"] = content_type

    return headers


def wait_for_document_intelligence(operation_url: str) -> dict:
    deadline = time.monotonic() + DOCUMENT_COMPARISON_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        response = requests.get(
            operation_url,
            headers=document_intelligence_headers(),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        status = clean_value(payload.get("status")).lower()

        if status == "succeeded":
            return payload.get("analyzeResult") or {}

        if status in {"failed", "canceled", "cancelled"}:
            error = payload.get("error") or {}
            raise RuntimeError(
                clean_value(error.get("message"))
                or "Azure Document Intelligence analysis failed."
            )

        time.sleep(1)

    raise TimeoutError(
        "Azure Document Intelligence did not finish before the comparison timeout."
    )


def analyze_document_layout(file_bytes: bytes) -> dict:
    if not document_intelligence_is_configured():
        raise RuntimeError(
            "Azure Document Intelligence endpoint and key are not configured."
        )

    analyze_url = (
        f"{DOCUMENT_INTELLIGENCE_ENDPOINT}/documentintelligence/"
        f"documentModels/prebuilt-layout:analyze"
        f"?api-version={quote(DOCUMENT_INTELLIGENCE_API_VERSION)}"
    )
    response = requests.post(
        analyze_url,
        headers=document_intelligence_headers("application/octet-stream"),
        data=file_bytes,
        timeout=60,
    )

    if response.status_code != 202:
        message = response.text
        try:
            error = response.json().get("error") or {}
            message = clean_value(error.get("message")) or message
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(
            f"Azure Document Intelligence rejected the document: {message}"
        )

    operation_url = response.headers.get("Operation-Location")

    if not operation_url:
        raise RuntimeError(
            "Azure Document Intelligence did not return an operation URL."
        )

    return wait_for_document_intelligence(operation_url)


def classify_document(file_bytes: bytes):
    if not DOCUMENT_INTELLIGENCE_CLASSIFIER_ID:
        return None

    analyze_url = (
        f"{DOCUMENT_INTELLIGENCE_ENDPOINT}/documentintelligence/"
        f"documentClassifiers/{quote(DOCUMENT_INTELLIGENCE_CLASSIFIER_ID)}:analyze"
        f"?api-version={quote(DOCUMENT_INTELLIGENCE_API_VERSION)}&splitMode=none"
    )
    response = requests.post(
        analyze_url,
        headers=document_intelligence_headers("application/octet-stream"),
        data=file_bytes,
        timeout=60,
    )

    if response.status_code != 202:
        message = response.text
        try:
            error = response.json().get("error") or {}
            message = clean_value(error.get("message")) or message
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(
            f"Azure Document Intelligence classifier rejected the document: {message}"
        )

    operation_url = response.headers.get("Operation-Location")

    if not operation_url:
        raise RuntimeError(
            "Azure Document Intelligence classifier returned no operation URL."
        )

    analyze_result = wait_for_document_intelligence(operation_url)
    documents = analyze_result.get("documents") or []

    if not documents:
        return None

    best_match = max(
        documents,
        key=lambda item: float(item.get("confidence") or 0),
    )
    return {
        "documentType": normalize_document_type(best_match.get("docType")),
        "confidence": round(float(best_match.get("confidence") or 0), 4),
    }


def download_private_blob(blob_url: str) -> bytes:
    storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")

    if not storage_connection_string:
        raise RuntimeError("Azure Blob Storage is not configured.")

    parsed_url = urlparse(blob_url)
    path_parts = parsed_url.path.lstrip("/").split("/", 1)

    if len(path_parts) != 2:
        raise ValueError("The stored Azure Blob URL is invalid.")

    container_name = unquote(path_parts[0])
    blob_name = unquote(path_parts[1])
    blob_service = BlobServiceClient.from_connection_string(
        storage_connection_string
    )
    blob_client = blob_service.get_blob_client(container_name, blob_name)
    return blob_client.download_blob().readall()


def normalize_comparison_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def comparison_tokens(value: str) -> set[str]:
    return {
        token
        for token in normalize_comparison_text(value).split()
        if len(token) > 2
        and token not in DOCUMENT_COMPARISON_STOP_WORDS
        and not token.isdigit()
    }


def text_similarity(client_text: str, reference_text: str) -> float:
    client_tokens = comparison_tokens(client_text)
    reference_tokens = comparison_tokens(reference_text)

    if not client_tokens or not reference_tokens:
        return 0.0

    common = client_tokens.intersection(reference_tokens)
    union = client_tokens.union(reference_tokens)
    smaller_set_size = min(len(client_tokens), len(reference_tokens))
    jaccard = len(common) / len(union) if union else 0.0
    containment = len(common) / smaller_set_size if smaller_set_size else 0.0
    return round((jaccard * 0.35) + (containment * 0.65), 4)


def document_keyword_score(document_type: str, content: str) -> tuple[float, list[str]]:
    keywords = DOCUMENT_TYPE_KEYWORDS.get(document_type, [])

    if not keywords:
        return 0.5, []

    normalized_content = normalize_comparison_text(content)
    matched_keywords = [
        keyword
        for keyword in keywords
        if normalize_comparison_text(keyword) in normalized_content
    ]
    expected_hits = min(4, len(keywords))
    score = min(1.0, len(matched_keywords) / expected_hits)
    return round(score, 4), matched_keywords


def layout_similarity(client_result: dict, reference_result: dict) -> float:
    client_pages = client_result.get("pages") or []
    reference_pages = reference_result.get("pages") or []
    client_page_count = max(1, len(client_pages))
    reference_page_count = max(1, len(reference_pages))
    page_score = min(client_page_count, reference_page_count) / max(
        client_page_count,
        reference_page_count,
    )

    client_lines = sum(len(page.get("lines") or []) for page in client_pages)
    reference_lines = sum(
        len(page.get("lines") or []) for page in reference_pages
    )

    if client_lines and reference_lines:
        line_score = min(client_lines, reference_lines) / max(
            client_lines,
            reference_lines,
        )
    else:
        line_score = page_score

    return round((page_score * 0.6) + (line_score * 0.4), 4)


def build_document_comparison(
    document_type: str,
    client_bytes: bytes,
    reference_bytes: bytes,
) -> dict:
    client_hash = hashlib.sha256(client_bytes).hexdigest()
    reference_hash = hashlib.sha256(reference_bytes).hexdigest()

    if hmac.compare_digest(client_hash, reference_hash):
        return {
            "result": "Matched",
            "confidence": 1.0,
            "exactMatch": True,
            "textSimilarity": 1.0,
            "keywordScore": 1.0,
            "layoutSimilarity": 1.0,
            "predictedDocumentType": document_type,
            "classifierConfidence": 1.0,
            "matchedKeywords": [],
            "reasons": ["The submitted file is byte-for-byte identical to the admin reference."],
            "clientContentHash": client_hash,
            "referenceContentHash": reference_hash,
        }

    client_layout = analyze_document_layout(client_bytes)
    reference_layout = analyze_document_layout(reference_bytes)
    client_text = client_layout.get("content") or ""
    reference_text = reference_layout.get("content") or ""
    overlap_score = text_similarity(client_text, reference_text)
    keyword_score, matched_keywords = document_keyword_score(
        document_type,
        client_text,
    )
    structure_score = layout_similarity(client_layout, reference_layout)
    classifier_result = classify_document(client_bytes)
    predicted_document_type = None
    classifier_confidence = None

    if classifier_result:
        predicted_document_type = classifier_result["documentType"]
        classifier_confidence = classifier_result["confidence"]
        classifier_matches = predicted_document_type == document_type
        classifier_score = (
            classifier_confidence
            if classifier_matches
            else max(0.0, 1.0 - classifier_confidence)
        )
        confidence = (
            (classifier_score * 0.65)
            + (keyword_score * 0.2)
            + (overlap_score * 0.1)
            + (structure_score * 0.05)
        )
    else:
        classifier_matches = None
        confidence = (
            (keyword_score * 0.55)
            + (overlap_score * 0.3)
            + (structure_score * 0.15)
        )

    confidence = round(min(1.0, max(0.0, confidence)), 4)
    reasons = []

    if classifier_result:
        reasons.append(
            "Classifier identified the file as "
            f"{format_document_type(predicted_document_type)} "
            f"with {round(classifier_confidence * 100)}% confidence."
        )

    if matched_keywords:
        reasons.append(
            "Expected document signals found: " + ", ".join(matched_keywords[:6]) + "."
        )
    else:
        reasons.append("No strong expected document keywords were detected.")

    reasons.append(
        f"Text/template similarity is {round(overlap_score * 100)}%; "
        f"layout similarity is {round(structure_score * 100)}%."
    )

    if (
        classifier_result
        and not classifier_matches
        and classifier_confidence >= 0.75
    ):
        result = "NotMatched"
        reasons.insert(0, "The trained classifier identified a different document type.")
    elif confidence >= 0.78 and keyword_score >= 0.5:
        result = "Matched"
    elif confidence <= 0.3 and keyword_score <= 0.25:
        result = "NotMatched"
    else:
        result = "NeedsReview"

    return {
        "result": result,
        "confidence": confidence,
        "exactMatch": False,
        "textSimilarity": overlap_score,
        "keywordScore": keyword_score,
        "layoutSimilarity": structure_score,
        "predictedDocumentType": predicted_document_type,
        "classifierConfidence": classifier_confidence,
        "matchedKeywords": matched_keywords,
        "reasons": reasons,
        "clientContentHash": client_hash,
        "referenceContentHash": reference_hash,
    }


def hash_client_password(password: str) -> str:
    if not password:
        raise ValueError("Password cannot be empty.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )

    return "pbkdf2_sha256${iterations}${salt}${digest}".format(
        iterations=PASSWORD_HASH_ITERATIONS,
        salt=base64.urlsafe_b64encode(salt).decode("ascii"),
        digest=base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_client_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)

        if algorithm != "pbkdf2_sha256":
            return False

        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected_digest = base64.urlsafe_b64decode(digest_text.encode("ascii"))

        actual_digest = hashlib.pbkdf2_hmac(
            "sha256",
            (password or "").encode("utf-8"),
            salt,
            iterations,
        )

        return hmac.compare_digest(actual_digest, expected_digest)

    except (ValueError, TypeError):
        logging.warning("Invalid client password hash format.")
        return False


def validate_new_password(password: str) -> list[str]:
    errors = []

    if len(password or "") < 8:
        errors.append("Password must contain at least 8 characters.")

    if not re.search(r"[A-Z]", password or ""):
        errors.append("Password must contain at least one uppercase letter.")

    if not re.search(r"[a-z]", password or ""):
        errors.append("Password must contain at least one lowercase letter.")

    if not re.search(r"\d", password or ""):
        errors.append("Password must contain at least one number.")

    if not re.search(r"[^A-Za-z0-9]", password or ""):
        errors.append("Password must contain at least one special character.")

    return errors


def none_if_empty(value):
    value = clean_value(value)
    return value if value else None


def get_form_value(form, *keys):
    for key in keys:
        value = form.get(key)
        if value not in [None, ""]:
            return clean_value(value)
    return ""


def get_optional_form_value(form, *keys):
    value = get_form_value(form, *keys)
    return value if value else None


def normalize_co_borrowers(value) -> list[dict]:
    if value in [None, ""]:
        return []

    parsed = value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            logging.warning("Invalid co-borrowers JSON payload.")
            return []

    if isinstance(parsed, dict):
        parsed = (
            parsed.get("coBorrowers")
            or parsed.get("CoBorrowers")
            or parsed.get("co_borrowers")
            or parsed.get("additionalCoBorrowers")
            or []
        )

    if not isinstance(parsed, list):
        return []

    normalized = []

    for item in parsed:
        if not isinstance(item, dict):
            continue

        first_name = clean_value(
            item.get("firstName")
            or item.get("FirstName")
            or item.get("first_name")
        )
        middle_name = clean_value(
            item.get("middleName")
            or item.get("MiddleName")
            or item.get("middle_name")
        )
        last_name = clean_value(
            item.get("lastName")
            or item.get("LastName")
            or item.get("last_name")
        )
        phone_country_code = clean_value(
            item.get("phoneCountryCode")
            or item.get("PhoneCountryCode")
            or item.get("phone_country_code")
        )
        phone = clean_value(
            item.get("phone")
            or item.get("Phone")
            or item.get("mobile")
            or item.get("Mobile")
        )
        email = clean_value(item.get("email") or item.get("Email"))

        if not any([first_name, last_name, phone, email]):
            continue

        normalized.append({
            "firstName": first_name,
            "middleName": middle_name,
            "lastName": last_name,
            "phoneCountryCode": phone_country_code,
            "phone": phone,
            "email": email,
        })

    return normalized


def get_client_co_borrowers(cursor, client_id: int) -> list[dict]:
    if not client_id:
        return []

    cursor.execute("""
        SELECT
            FirstName,
            MiddleName,
            LastName,
            PhoneCountryCode,
            Phone,
            Email
        FROM dbo.ClientCoBorrowers
        WHERE ClientId = ?
        ORDER BY SortOrder, Id
    """, client_id)

    return [
        {
            "firstName": clean_value(row.FirstName),
            "middleName": clean_value(row.MiddleName),
            "lastName": clean_value(row.LastName),
            "phoneCountryCode": clean_value(row.PhoneCountryCode),
            "phone": clean_value(row.Phone),
            "email": clean_value(row.Email),
        }
        for row in cursor.fetchall()
    ]


def replace_client_co_borrowers(
    cursor,
    client_id: int,
    co_borrowers: list[dict],
) -> None:
    cursor.execute(
        "DELETE FROM dbo.ClientCoBorrowers WHERE ClientId = ?",
        client_id,
    )

    for sort_order, co_borrower in enumerate(co_borrowers, start=1):
        cursor.execute("""
            INSERT INTO dbo.ClientCoBorrowers (
                ClientId,
                SortOrder,
                FirstName,
                MiddleName,
                LastName,
                PhoneCountryCode,
                Phone,
                Email
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client_id,
            sort_order,
            co_borrower.get("firstName", ""),
            co_borrower.get("middleName", ""),
            co_borrower.get("lastName", ""),
            co_borrower.get("phoneCountryCode", ""),
            co_borrower.get("phone", ""),
            co_borrower.get("email", ""),
        ))


def extract_ghl_contact_id(ghl_sync):
    body = ghl_sync.get("body") if isinstance(ghl_sync, dict) else None

    if not isinstance(body, dict):
        return ""

    direct_keys = ["id", "_id", "contactId", "contact_id"]
    for key in direct_keys:
        value = clean_value(body.get(key))
        if value:
            return value

    contact = body.get("contact")
    if isinstance(contact, dict):
        for key in direct_keys:
            value = clean_value(contact.get(key))
            if value:
                return value

    data = body.get("data")
    if isinstance(data, dict):
        for key in direct_keys:
            value = clean_value(data.get(key))
            if value:
                return value

        contact = data.get("contact")
        if isinstance(contact, dict):
            for key in direct_keys:
                value = clean_value(contact.get(key))
                if value:
                    return value

    return ""


def normalize_transaction_type(transaction_type: str) -> str:
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        clean_value(transaction_type).lower(),
    ).strip("_")

    aliases = {
        "alt": "alt_doc",
        "alt_doc": "alt_doc",
        "altdoc": "alt_doc",
        "alternative_doc": "alt_doc",
        "alternative_document": "alt_doc",
        "full": "full_doc",
        "full_doc": "full_doc",
        "fulldoc": "full_doc",
        "full_document": "full_doc",
    }

    return aliases.get(normalized, normalized)


DOCUMENT_TYPE_ALIASES = {
    "id": "id",
    "id_file": "id",
    "valid_id": "id",
    "identification": "id",
    "identity_document": "id",
    "passport": "passport",
    "passport_file": "passport",
    "payslip": "payslip",
    "payslip_file": "payslip",
    "pay_slip": "payslip",
    "bas": "bas-from-ato-portal",
    "bas_from_ato": "bas-from-ato-portal",
    "bas_from_ato_portal": "bas-from-ato-portal",
    "business_banking_statement": "business-banking-statements",
    "business_banking_statements": "business-banking-statements",
    "management_report": "management-reports-financial-statements",
    "management_reports": "management-reports-financial-statements",
    "financial_statement": "management-reports-financial-statements",
    "financial_statements": "management-reports-financial-statements",
    "management_reports_financial_statements": "management-reports-financial-statements",
    "group_certificate": "group-certificate-payment-summary",
    "payment_summary": "group-certificate-payment-summary",
    "group_certificate_payment_summary": "group-certificate-payment-summary",
    "company_tax_return": "company-tax-returns",
    "company_tax_returns": "company-tax-returns",
    "individual_tax_return": "individual-tax-returns",
    "individual_tax_returns": "individual-tax-returns",
    "last_6_months_mortgage_statement": "last-6-months-mortgage-statements",
    "last_6_months_mortgage_statements": "last-6-months-mortgage-statements",
    "last_six_months_mortgage_statements": "last-6-months-mortgage-statements",
    "council_rate_notice": "council-rates-notice",
    "council_rates_notice": "council-rates-notice",
}


def normalize_document_type(document_type: str) -> str:
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        clean_value(document_type).lower(),
    ).strip("_")

    if not normalized:
        return ""

    if normalized.endswith("_file"):
        normalized = normalized[:-5]

    return DOCUMENT_TYPE_ALIASES.get(
        normalized,
        normalized.replace("_", "-"),
    )


def get_required_documents(transaction_type: str) -> list[str]:
    return list(
        TRANSACTION_DOCUMENTS.get(
            normalize_transaction_type(transaction_type),
            [],
        )
    )


def get_client_waived_documents(cursor, client_id: int) -> list[str]:
    if not client_id:
        return []

    cursor.execute("""
        SELECT DocumentType
        FROM dbo.DocumentWaivers
        WHERE ClientId = ?
        ORDER BY WaivedAt ASC, Id ASC
    """, client_id)

    return sorted({
        normalize_document_type(row.DocumentType)
        for row in cursor.fetchall()
        if clean_value(row.DocumentType)
    })


def is_valid_document_type(transaction_type: str, document_type: str) -> bool:
    normalized_document_type = normalize_document_type(document_type)

    if not normalized_document_type:
        return True

    return normalized_document_type in get_required_documents(transaction_type)


def format_document_type(document_type: str) -> str:
    clean_type = normalize_document_type(document_type)
    return DOCUMENT_LABELS.get(
        clean_type,
        clean_type.replace("-", " ").title() if clean_type else "Document",
    )


def get_required_document_types(transaction_type: str) -> list[str]:
    normalized_transaction_type = normalize_ghl_key(transaction_type)

    if normalized_transaction_type not in TRANSACTION_DOCUMENTS:
        return []

    return list(TRANSACTION_DOCUMENTS[normalized_transaction_type])


def get_required_document_labels(transaction_type: str) -> list[str]:
    return [
        format_document_type(document_type)
        for document_type in get_required_document_types(transaction_type)
    ]


def format_required_documents_for_ghl(transaction_type: str) -> str:
    labels = get_required_document_labels(transaction_type)

    if not labels:
        return ""

    return "\n".join(f"• {label}" for label in labels)


def format_lead_type(lead_type: str) -> str:
    clean_type = (lead_type or "broker").strip().lower()
    return LEAD_TYPE_LABELS.get(clean_type, clean_type.replace("-", " ").title())


def get_client_document_status(cursor, client_id: int):
    cursor.execute("""
        SELECT TOP 1 TransactionType
        FROM Clients
        WHERE Id = ?
    """, client_id)

    client_row = cursor.fetchone()
    transaction_type = (
        clean_value(client_row.TransactionType)
        if client_row
        else ""
    )
    required_documents = get_required_documents(transaction_type)
    waived_documents = get_client_waived_documents(cursor, client_id)

    cursor.execute("""
        SELECT
            DocumentType,
            COALESCE(Status, 'Pending') AS Status
        FROM Documents
        WHERE ClientId = ?
    """, client_id)

    rows = cursor.fetchall()

    uploaded_raw = [
        normalize_document_type(row.DocumentType)
        for row in rows
        if row.DocumentType
    ]

    verified_raw = [
        normalize_document_type(row.DocumentType)
        for row in rows
        if row.DocumentType and (row.Status or "").strip().lower() == "verified"
    ]

    uploaded_documents = sorted(set(uploaded_raw))
    verified_documents = sorted(set(verified_raw))

    missing_documents = [
        document_type
        for document_type in required_documents
        if document_type not in uploaded_documents
        and document_type not in waived_documents
    ]

    unverified_documents = [
        document_type
        for document_type in required_documents
        if document_type in uploaded_documents
        and document_type not in verified_documents
        and document_type not in waived_documents
    ]

    satisfied_documents = sorted({
        document_type
        for document_type in required_documents
        if document_type in verified_documents
        or document_type in waived_documents
    })

    progress = (
        round((len([
            item for item in satisfied_documents if item in required_documents
        ]) / len(required_documents)) * 100)
        if required_documents
        else 0
    )
    is_complete = (
        bool(required_documents)
        and len(missing_documents) == 0
        and len(unverified_documents) == 0
    )

    return {
        "transactionType": transaction_type,
        "requiredDocuments": required_documents,
        "uploadedDocuments": uploaded_documents,
        "verifiedDocuments": verified_documents,
        "waivedDocuments": waived_documents,
        "satisfiedDocuments": satisfied_documents,
        "missingDocuments": missing_documents,
        "unverifiedDocuments": unverified_documents,
        "isComplete": is_complete,
        "progress": progress,
        "documentStatus": "Complete" if is_complete else "Incomplete",
    }



def update_client_workflow_status(cursor, client_id: int):
    status_info = get_client_document_status(cursor, client_id)
    completed_date_value = datetime.utcnow() if status_info["isComplete"] else None

    cursor.execute("""
        UPDATE Clients
        SET
            Progress = ?,
            CompletedDate = CASE WHEN ? = 1 THEN COALESCE(CompletedDate, ?) ELSE NULL END,
            Status = CASE WHEN ? = 1 THEN 'Documents Complete' ELSE COALESCE(Status, 'Pending Team Call') END
        WHERE Id = ?
    """, (
        status_info["progress"],
        1 if status_info["isComplete"] else 0,
        completed_date_value,
        1 if status_info["isComplete"] else 0,
        client_id,
    ))

    return status_info


def get_ghl_headers():
    token = os.getenv("GHL_ACCESS_TOKEN", "").strip()

    if not token:
        raise ValueError("GHL_ACCESS_TOKEN is not configured.")

    authorization = (
        token
        if token.lower().startswith("bearer ")
        else f"Bearer {token}"
    )

    return {
        "Authorization": authorization,
        "Accept": "application/json",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }


def add_ghl_tags(contact_id: str, tags: list[str]) -> dict:
    contact_id = clean_value(contact_id)
    clean_tags = list(dict.fromkeys(
        clean_value(tag) for tag in tags if clean_value(tag)
    ))

    if not contact_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL contact ID.",
        }

    if not clean_tags:
        return {
            "success": False,
            "skipped": True,
            "message": "No GHL tags supplied.",
        }

    ghl_api_base = os.getenv(
        "GHL_API_BASE",
        "https://services.leadconnectorhq.com",
    ).rstrip("/")

    try:
        response = requests.post(
            f"{ghl_api_base}/contacts/{contact_id}/tags",
            headers=get_ghl_headers(),
            json={"tags": clean_tags},
            timeout=30,
        )

        logging.info(
            "GHL add tags response: %s %s",
            response.status_code,
            response.text[:1000],
        )

        try:
            body = response.json()
        except ValueError:
            body = response.text

        return {
            "success": response.status_code in [200, 201],
            "statusCode": response.status_code,
            "body": body,
        }

    except requests.RequestException as exc:
        logging.exception("Failed to add GHL tags.")
        return {
            "success": False,
            "statusCode": 500,
            "message": str(exc),
        }



def remove_ghl_tags(contact_id: str, tags: list[str]) -> dict:
    contact_id = clean_value(contact_id)
    clean_tags = list(dict.fromkeys(
        clean_value(tag) for tag in tags if clean_value(tag)
    ))

    if not contact_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL contact ID.",
        }

    if not clean_tags:
        return {
            "success": False,
            "skipped": True,
            "message": "No GHL tags supplied.",
        }

    ghl_api_base = os.getenv(
        "GHL_API_BASE",
        "https://services.leadconnectorhq.com",
    ).rstrip("/")

    try:
        response = requests.delete(
            f"{ghl_api_base}/contacts/{contact_id}/tags",
            headers=get_ghl_headers(),
            json={"tags": clean_tags},
            timeout=30,
        )

        logging.info(
            "GHL remove tags response: %s %s",
            response.status_code,
            response.text[:1000],
        )

        try:
            body = response.json()
        except ValueError:
            body = response.text

        return {
            "success": response.status_code in [200, 201, 204],
            "statusCode": response.status_code,
            "body": body,
        }

    except requests.RequestException as exc:
        logging.exception("Failed to remove GHL tags.")
        return {
            "success": False,
            "statusCode": 500,
            "message": str(exc),
        }


def remove_contact_from_ghl_workflow(
    contact_id: str,
    workflow_id: str,
) -> dict:
    contact_id = clean_value(contact_id)
    workflow_id = clean_value(workflow_id)

    if not contact_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL contact ID.",
        }

    if not workflow_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL workflow ID.",
        }

    ghl_api_base = os.getenv(
        "GHL_API_BASE",
        "https://services.leadconnectorhq.com",
    ).rstrip("/")

    url = f"{ghl_api_base}/contacts/{contact_id}/workflow/{workflow_id}"

    try:
        response = requests.delete(
            url,
            headers=get_ghl_headers(),
            timeout=30,
        )

        logging.info(
            "GHL remove workflow | contact=%s workflow=%s status=%s body=%s",
            contact_id,
            workflow_id,
            response.status_code,
            response.text[:2000],
        )

        try:
            body = response.json()
        except ValueError:
            body = response.text

        return {
            "success": response.status_code in [200, 201, 204],
            "statusCode": response.status_code,
            "body": body,
            "contactId": contact_id,
            "workflowId": workflow_id,
        }

    except requests.RequestException as exc:
        logging.exception("Failed to remove contact from GHL workflow.")
        return {
            "success": False,
            "statusCode": 500,
            "message": str(exc),
            "contactId": contact_id,
            "workflowId": workflow_id,
        }


def add_contact_to_ghl_workflow(
    contact_id: str,
    workflow_id: str,
) -> dict:
    contact_id = clean_value(contact_id)
    workflow_id = clean_value(workflow_id)

    if not contact_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL contact ID.",
        }

    if not workflow_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL workflow ID.",
        }

    ghl_api_base = os.getenv(
        "GHL_API_BASE",
        "https://services.leadconnectorhq.com",
    ).rstrip("/")

    url = f"{ghl_api_base}/contacts/{contact_id}/workflow/{workflow_id}"

    try:
        response = requests.post(
            url,
            headers=get_ghl_headers(),
            json={},
            timeout=30,
        )

        logging.info(
            "GHL add workflow | contact=%s workflow=%s status=%s body=%s",
            contact_id,
            workflow_id,
            response.status_code,
            response.text[:2000],
        )

        try:
            body = response.json()
        except ValueError:
            body = response.text

        return {
            "success": response.status_code in [200, 201, 204],
            "statusCode": response.status_code,
            "body": body,
            "contactId": contact_id,
            "workflowId": workflow_id,
        }

    except requests.RequestException as exc:
        logging.exception("Failed to add contact to GHL workflow.")
        return {
            "success": False,
            "statusCode": 500,
            "message": str(exc),
            "contactId": contact_id,
            "workflowId": workflow_id,
        }


def retrigger_ghl_tag(contact_id: str, tag: str) -> dict:
    remove_result = remove_ghl_tags(contact_id, [tag])

    logging.info(
        "GHL tag removal | contact=%s tag=%s result=%s",
        contact_id,
        tag,
        json.dumps(remove_result, default=str)[:2000],
    )

    time.sleep(2)

    add_result = add_ghl_tags(contact_id, [tag])

    logging.info(
        "GHL tag addition | contact=%s tag=%s result=%s",
        contact_id,
        tag,
        json.dumps(add_result, default=str)[:2000],
    )

    return {
        "success": bool(add_result.get("success")),
        "tag": tag,
        "remove": remove_result,
        "add": add_result,
    }


def start_submission_workflow(contact_id: str) -> dict:
    """
    Start the submission workflow directly through the HighLevel workflow API.

    Direct enrollment avoids relying only on a tag event. If the contact is
    already active, the old execution is removed and enrollment is retried.
    """
    contact_id = clean_value(contact_id)

    if not contact_id:
        return {
            "success": False,
            "skipped": True,
            "message": "Missing GHL contact ID.",
        }

    if not GHL_CLIENT_SUBMISSION_WORKFLOW_ID:
        logging.warning(
            "GHL_CLIENT_SUBMISSION_WORKFLOW_ID is missing; using tag trigger fallback."
        )
        fallback = retrigger_ghl_tag(
            contact_id,
            GHL_CLIENT_SUBMISSION_TAG,
        )
        return {
            "success": bool(fallback.get("success")),
            "mode": "tag-fallback",
            "workflowId": None,
            "tagTrigger": fallback,
        }

    workflow_id = GHL_CLIENT_SUBMISSION_WORKFLOW_ID

    removal_result = remove_contact_from_ghl_workflow(
        contact_id,
        workflow_id,
    )

    logging.info(
        "Submission workflow removal result: %s",
        json.dumps(removal_result, default=str)[:3000],
    )

    time.sleep(3)

    attempts = []
    retry_delays = [0, 3, 5, 8]

    for attempt_number, delay_seconds in enumerate(retry_delays, start=1):
        if delay_seconds:
            time.sleep(delay_seconds)

        add_result = add_contact_to_ghl_workflow(
            contact_id,
            workflow_id,
        )
        attempts.append(add_result)

        logging.info(
            "Submission workflow enrollment attempt %s: %s",
            attempt_number,
            json.dumps(add_result, default=str)[:3000],
        )

        if add_result.get("success"):
            tag_result = add_ghl_tags(
                contact_id,
                [GHL_CLIENT_SUBMISSION_TAG],
            )

            return {
                "success": True,
                "mode": "direct-workflow",
                "workflowId": workflow_id,
                "workflowRemoval": removal_result,
                "workflowEnrollment": add_result,
                "attempts": attempts,
                "reportingTag": tag_result,
            }

        if add_result.get("statusCode") not in [400, 409, 422]:
            break

    return {
        "success": False,
        "mode": "direct-workflow",
        "workflowId": workflow_id,
        "message": "HighLevel did not enroll the contact in the submission workflow.",
        "workflowRemoval": removal_result,
        "attempts": attempts,
    }



def normalize_ghl_key(value):
    raw = (value or "").strip()

    if not raw:
        return ""

    raw = raw.replace("{{", "").replace("}}", "")
    raw = raw.replace("contact.", "")
    raw = raw.replace("customFields.", "")
    raw = raw.replace("custom_fields.", "")
    raw = raw.strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")

    return raw


def normalize_ghl_field_value(field_key, value):
    value = clean_value(value)

    if not value:
        return ""

    normalized_key = normalize_ghl_key(field_key)
    normalized_value = normalize_ghl_key(value)

    dropdown_value_map = {
        "classification_type": {
            "residential": "residential",
            "commercial": "commercial",
        },
        "borrower_type": {
            "individual": "individual",
            "company": "company",
        },
        "objective": {
            "purchase": "purchase",
            "refinance": "refinance",
            "refinancing": "refinance",
            "asset_finance": "asset_finance",
            "construction": "construction",
            "development": "development",
            "personal_loan": "personal_loan",
            "business_loan": "business_loan",
        },
        "loan_type": {
            "residential": "residential",
            "commercial": "commercial",
        },
        "purpose": {
            "investment": "investment",
            "owner_occupied": "owner_occupied",
        },
        "transaction_type": {
            "alt_doc": "alt_doc",
            "full_doc": "full_doc",
        },
        "application_source": {
            "broker": "broker",
            "referral": "referral",
            "direct_client": "direct_client",
            "directclient": "direct_client",
        },
        "with_borrowers_guarantors": {
            "yes": "yes",
            "no": "no",
        },
        "with_borrowers__guarantors": {
            "yes": "yes",
            "no": "no",
        },
    }

    return dropdown_value_map.get(normalized_key, {}).get(normalized_value, value)


def extract_ghl_custom_fields(response_body):
    if isinstance(response_body, list):
        return response_body

    if not isinstance(response_body, dict):
        return []

    for key in [
        "customFields",
        "custom_fields",
        "fields",
        "data",
        "items",
    ]:
        fields = response_body.get(key)

        if isinstance(fields, list):
            return fields

    return []


def get_ghl_custom_field_map(ghl_api_base, location_id):
    global _GHL_FIELD_MAP_CACHE

    if _GHL_FIELD_MAP_CACHE is not None:
        return _GHL_FIELD_MAP_CACHE

    field_map = {}

    if not ghl_api_base or not location_id:
        return field_map

    try:
        response = requests.get(
            f"{ghl_api_base}/locations/{location_id}/customFields",
            headers=get_ghl_headers(),
            timeout=30,
        )

        logging.info(
            "GHL custom fields response: %s %s",
            response.status_code,
            response.text[:1000],
        )

        if response.status_code not in [200, 201]:
            return field_map

        response_body = response.json()
        fields = extract_ghl_custom_fields(response_body)

        for field in fields:
            if not isinstance(field, dict):
                continue

            field_id = clean_value(
                field.get("id")
                or field.get("_id")
                or field.get("fieldId")
                or field.get("field_id")
            )

            if not field_id:
                continue

            raw_keys = [
                field.get("fieldKey"),
                field.get("field_key"),
                field.get("key"),
                field.get("name"),
                field.get("fieldName"),
                field.get("field_name"),
            ]

            for raw_key in raw_keys:
                normalized_key = normalize_ghl_key(str(raw_key or ""))

                if normalized_key:
                    field_map[normalized_key] = field_id

    except Exception:
        logging.exception("Failed to load GHL custom fields.")

    _GHL_FIELD_MAP_CACHE = field_map
    return field_map


def get_ghl_field_id(env_name, ghl_field_map=None):
    env_field_id = os.getenv(env_name, "").strip()

    if env_field_id:
        return env_field_id

    for field_key in GHL_CUSTOM_FIELD_CONFIG.get(env_name, []):
        normalized_key = normalize_ghl_key(field_key)

        if normalized_key and ghl_field_map and normalized_key in ghl_field_map:
            return ghl_field_map[normalized_key]

    return ""


def add_ghl_field(custom_fields, env_name, field_value, ghl_field_map=None):
    if field_value in [None, ""]:
        return

    field_id = get_ghl_field_id(env_name, ghl_field_map)

    if not field_id:
        logging.warning("GHL custom field skipped. Missing field ID for %s.", env_name)
        return

    field_key = (
        GHL_CUSTOM_FIELD_CONFIG.get(env_name, [env_name])[0]
        if GHL_CUSTOM_FIELD_CONFIG.get(env_name)
        else env_name
    )

    custom_fields.append({
        "id": field_id,
        "field_value": normalize_ghl_field_value(field_key, field_value),
    })


def build_ghl_custom_fields(
    unique_id,
    document_status,
    missing_documents,
    required_documents,
    source,
    classification_type,
    borrower_type,
    objective,
    loan_type,
    purpose,
    transaction_type,
    with_borrowers_guarantors,
    anticipated_settlement_date,
    referrer_first_name,
    referrer_middle_name,
    referrer_last_name,
    referrer_phone,
    referrer_email,
    ghl_field_map=None,
):
    custom_fields = []

    add_ghl_field(
        custom_fields,
        "GHL_CUSTOM_FIELD_CLIENT_ID",
        unique_id,
        ghl_field_map,
    )

    add_ghl_field(
        custom_fields,
        "GHL_CUSTOM_FIELD_DOCUMENT_STATUS",
        document_status,
        ghl_field_map,
    )

    add_ghl_field(
        custom_fields,
        "GHL_CUSTOM_FIELD_MISSING_DOCUMENTS",
        ", ".join(missing_documents),
        ghl_field_map,
    )

    add_ghl_field(
        custom_fields,
        "GHL_CUSTOM_FIELD_REQUIRED_DOCUMENTS",
        required_documents,
        ghl_field_map,
    )

    add_ghl_field(custom_fields, "GHL_APPLICATION_SOURCE_FIELD", source, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_CLASSIFICATION_FIELD", classification_type, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_BORROWER_FIELD", borrower_type, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_OBJECTIVE_FIELD", objective, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_LOAN_TYPE_FIELD", loan_type, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_PURPOSE_FIELD", purpose, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_TRANSACTION_FIELD", transaction_type, ghl_field_map)
    add_ghl_field(
        custom_fields,
        "GHL_WITH_BORROWERS_GUARANTORS_FIELD",
        with_borrowers_guarantors,
        ghl_field_map,
    )
    add_ghl_field(
        custom_fields,
        "GHL_SETTLEMENT_FIELD",
        anticipated_settlement_date,
        ghl_field_map,
    )

    add_ghl_field(custom_fields, "GHL_REFERRER_FIRST_NAME_FIELD", referrer_first_name, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_REFERRER_MIDDLE_NAME_FIELD", referrer_middle_name, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_REFERRER_LAST_NAME_FIELD", referrer_last_name, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_REFERRER_PHONE_FIELD", referrer_phone, ghl_field_map)
    add_ghl_field(custom_fields, "GHL_REFERRER_EMAIL_FIELD", referrer_email, ghl_field_map)

    return custom_fields

def sync_client_to_ghl(
    unique_id,
    first_name,
    middle_name,
    last_name,
    email,
    phone,
    lead_type,
    source,
    classification_type,
    borrower_type,
    objective,
    loan_type,
    purpose,
    transaction_type,
    with_borrowers_guarantors,
    anticipated_settlement_date,
    referrer_first_name,
    referrer_middle_name,
    referrer_last_name,
    referrer_phone,
    referrer_email,
    uploaded_documents,
    missing_documents,
):
    try:
        ghl_api_base = os.getenv(
            "GHL_API_BASE",
            "https://services.leadconnectorhq.com",
        ).rstrip("/")

        ghl_token = os.getenv("GHL_ACCESS_TOKEN", "").strip()
        location_id = os.getenv("GHL_LOCATION_ID", "").strip()

        if not ghl_token:
            logging.warning("GHL sync skipped. Missing GHL_ACCESS_TOKEN.")
            return {
                "success": False,
                "skipped": True,
                "message": "Missing GHL_ACCESS_TOKEN.",
            }

        if not location_id:
            logging.warning("GHL sync skipped. Missing GHL_LOCATION_ID.")
            return {
                "success": False,
                "skipped": True,
                "message": "Missing GHL_LOCATION_ID.",
            }

        if not email:
            logging.warning("GHL sync skipped. Missing client email.")
            return {
                "success": False,
                "skipped": True,
                "message": "Missing client email.",
            }

        uploaded_labels = [
            format_document_type(document_type)
            for document_type in uploaded_documents
        ]

        missing_labels = [
            format_document_type(document_type)
            for document_type in missing_documents
        ]

        required_labels = get_required_document_labels(transaction_type)
        required_documents_text = format_required_documents_for_ghl(
            transaction_type
        )

        is_complete = len(missing_documents) == 0
        document_status = "Complete" if is_complete else "Incomplete"
        lead_label = format_lead_type(lead_type)
        source_label = format_lead_type(source)

        full_name = " ".join(
            filter(None, [first_name, middle_name, last_name])
        ).strip()

        tags = [
            "Azure Client Portal",
            "Website Intake",
            lead_label,
            source_label,
            "Pending Team Call",
            "Documents Complete" if is_complete else "Documents Incomplete",
        ]

        for label in uploaded_labels:
            tags.append(f"Uploaded: {label}")

        for label in missing_labels:
            tags.append(f"Missing: {label}")

        payload = {
            "locationId": location_id,
            "firstName": first_name or "",
            "lastName": last_name or "",
            "name": full_name,
            "email": email,
            "phone": phone or "",
            "source": source_label or "Website Intake",
            "tags": tags,
        }

        ghl_field_map = get_ghl_custom_field_map(ghl_api_base, location_id)

        custom_fields = build_ghl_custom_fields(
            unique_id=unique_id,
            document_status=document_status,
            missing_documents=missing_labels,
            required_documents=required_documents_text,
            source=source_label,
            classification_type=classification_type,
            borrower_type=borrower_type,
            objective=objective,
            loan_type=loan_type,
            purpose=purpose,
            transaction_type=transaction_type,
            with_borrowers_guarantors=with_borrowers_guarantors,
            anticipated_settlement_date=anticipated_settlement_date,
            referrer_first_name=referrer_first_name,
            referrer_middle_name=referrer_middle_name,
            referrer_last_name=referrer_last_name,
            referrer_phone=referrer_phone,
            referrer_email=referrer_email,
            ghl_field_map=ghl_field_map,
        )

        if custom_fields:
            payload["customFields"] = custom_fields

        response = requests.post(
            f"{ghl_api_base}/contacts/upsert",
            headers=get_ghl_headers(),
            json=payload,
            timeout=30,
        )

        logging.info(
            "GHL sync response: %s %s",
            response.status_code,
            response.text,
        )

        try:
            parsed_body = response.json()
        except Exception:
            parsed_body = response.text

        return {
            "success": response.status_code in [200, 201],
            "statusCode": response.status_code,
            "body": parsed_body,
        }

    except Exception as e:
        logging.exception("GHL sync failed.")
        return {
            "success": False,
            "statusCode": 500,
            "message": str(e),
        }


@app.route(route="ghl-custom-fields", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "OPTIONS"])
def debug_ghl_custom_fields(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        ghl_api_base = os.getenv(
            "GHL_API_BASE",
            "https://services.leadconnectorhq.com",
        ).rstrip("/")
        location_id = os.getenv("GHL_LOCATION_ID", "").strip()

        field_map = get_ghl_custom_field_map(ghl_api_base, location_id)

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "fieldMap": field_map,
                "mappedKeys": sorted(field_map.keys()),
                "count": len(field_map),
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Fetch GHL custom fields failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e),
            }),
            status_code=500,
            mimetype="application/json"
        ))


@app.route(route="login", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "OPTIONS"])
def login(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        data = req.get_json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not username or not password:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Username and password are required."
                }),
                status_code=400,
                mimetype="application/json"
            ))

        conn = get_sql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT Id, Username, Role
            FROM Users
            WHERE Username = ?
            AND Password = ?
        """, (username, password))

        user = cursor.fetchone()

        cursor.close()
        conn.close()

        if not user:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Invalid username or password."
                }),
                status_code=401,
                mimetype="application/json"
            ))

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Login successful.",
                "user": {
                    "id": user.Id,
                    "username": user.Username,
                    "role": user.Role
                }
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Login failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e)
            }),
            status_code=500,
            mimetype="application/json"
        ))


@app.route(route="uploadclient", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "OPTIONS"])
def uploadclient(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        form = req.form
        files = req.files
        uploaded_file = files.get("file")

        existing_unique_id = clean_value(form.get("uniqueId"))
        is_initial_submission = not bool(existing_unique_id)

        first_name = clean_value(form.get("firstName"))
        middle_name = clean_value(form.get("middleName"))
        last_name = clean_value(form.get("lastName"))
        email = clean_value(form.get("email"))
        phone = clean_value(form.get("phone"))

        lead_type = clean_value(form.get("leadType") or form.get("source") or "Broker")
        source = clean_value(form.get("source") or lead_type or "Broker")

        raw_document_type = get_form_value(
            form,
            "documentType",
            "DocumentType",
            "document_type",
        )
        document_type = normalize_document_type(raw_document_type)
        uploaded_filename = uploaded_file.filename if uploaded_file else ""
        blob_url = ""

        classification_type = get_form_value(form, "classificationType", "ClassificationType", "classification_type")
        borrower_type = get_form_value(form, "borrowerType", "BorrowerType", "borrower_type")
        objective = get_form_value(form, "objective", "Objective")
        loan_type = get_form_value(form, "loanType", "LoanType", "loan_type")
        purpose = get_form_value(form, "purpose", "Purpose")
        transaction_type = get_form_value(form, "transactionType", "TransactionType", "transaction_type")
        with_borrowers_guarantors = get_form_value(
            form,
            "withBorrowersGuarantors",
            "WithBorrowersGuarantors",
            "with_borrowers_guarantors",
            "withBorrowers",
            "WithBorrowers",
        )
        co_borrower_field_names = (
            "coBorrowers",
            "CoBorrowers",
            "co_borrowers",
            "coBorrowersJson",
            "CoBorrowersJson",
            "additionalCoBorrowers",
            "AdditionalCoBorrowers",
            "additional_co_borrowers",
        )
        co_borrowers_were_supplied = any(
            field_name in form for field_name in co_borrower_field_names
        )
        co_borrowers = normalize_co_borrowers(
            get_form_value(form, *co_borrower_field_names)
        )
        anticipated_settlement_date = get_form_value(
            form,
            "anticipatedSettlementDate",
            "AnticipatedSettlementDate",
            "anticipated_settlement_date",
        )

        veda_issues = get_form_value(form, "vedaIssues", "VedaIssues", "veda_issues")
        conduct_issues = get_form_value(form, "conductIssues", "ConductIssues", "conduct_issues")
        client_needs_objectives = get_form_value(
            form,
            "clientNeedsObjectives",
            "ClientNeedsObjectives",
            "client_needs_objectives",
        )
        applicant_background = get_form_value(
            form,
            "applicantBackground",
            "ApplicantBackground",
            "applicant_background",
        )
        explanation_of_income = get_form_value(
            form,
            "explanationOfIncome",
            "ExplanationOfIncome",
            "explanation_of_income",
        )
        security = get_form_value(form, "security", "Security")
        loan_amount = get_optional_form_value(form, "loanAmount", "LoanAmount", "loan_amount")
        security_value = get_optional_form_value(form, "securityValue", "SecurityValue", "security_value")
        lvr = get_optional_form_value(form, "lvr", "Lvr", "LVR")
        special_notes = get_form_value(form, "specialNotes", "SpecialNotes", "special_notes")

        required_values = {
            "email": email,
            "phone": phone,
            "source": source,
            "classificationType": classification_type,
            "borrowerType": borrower_type,
            "objective": objective,
            "loanType": loan_type,
            "purpose": purpose,
            "transactionType": transaction_type,
            "anticipatedSettlementDate": anticipated_settlement_date,
        }

        missing_required_fields = [
            REQUIRED_INTAKE_FIELDS[field_name]
            for field_name, field_value in required_values.items()
            if not field_value
        ]

        if missing_required_fields:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Missing required fields.",
                    "missingFields": missing_required_fields,
                }),
                status_code=400,
                mimetype="application/json"
            ))

        if (
            is_initial_submission
            and with_borrowers_guarantors.strip().lower() == "yes"
            and not co_borrowers
        ):
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Add at least one co-borrower before submitting.",
                    "field": "coBorrowers",
                }),
                status_code=400,
                mimetype="application/json"
            ))

        required_documents = get_required_documents(transaction_type)

        if not required_documents:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Unsupported transaction type.",
                    "transactionType": transaction_type,
                }),
                status_code=400,
                mimetype="application/json"
            ))

        normalized_transaction_type = normalize_transaction_type(transaction_type)

        logging.info(
            "Upload validation | transaction raw=%s normalized=%s | "
            "document raw=%s normalized=%s | allowed=%s",
            transaction_type,
            normalized_transaction_type,
            raw_document_type,
            document_type,
            required_documents,
        )

        if document_type and not is_valid_document_type(
            normalized_transaction_type,
            document_type,
        ):
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "The selected document type is not valid for this transaction type.",
                    "transactionType": transaction_type,
                    "normalizedTransactionType": normalized_transaction_type,
                    "documentType": raw_document_type,
                    "normalizedDocumentType": document_type,
                    "allowedDocumentTypes": required_documents,
                }),
                status_code=400,
                mimetype="application/json"
            ))

        referrer_first_name = get_form_value(form, "referrerFirstName", "ReferrerFirstName", "referrer_first_name")
        referrer_middle_name = get_form_value(form, "referrerMiddleName", "ReferrerMiddleName", "referrer_middle_name")
        referrer_last_name = get_form_value(form, "referrerLastName", "ReferrerLastName", "referrer_last_name")
        referrer_phone = get_form_value(form, "referrerPhone", "ReferrerPhone", "referrer_phone")
        referrer_email = get_form_value(form, "referrerEmail", "ReferrerEmail", "referrer_email")

        if source.lower() == "direct-client":
            referrer_first_name = ""
            referrer_middle_name = ""
            referrer_last_name = ""
            referrer_phone = ""
            referrer_email = ""

        conn = get_sql_connection()
        cursor = conn.cursor()

        if existing_unique_id:
            cursor.execute("""
                SELECT TOP 1
                    Id,
                    UniqueId,
                    FirstName,
                    MiddleName,
                    LastName,
                    Email
                FROM Clients
                WHERE UniqueId = ?
            """, existing_unique_id)

            existing_client = cursor.fetchone()

            if not existing_client:
                cursor.close()
                conn.close()

                return add_cors(func.HttpResponse(
                    json.dumps({
                        "success": False,
                        "message": "Client Unique ID not found."
                    }),
                    status_code=404,
                    mimetype="application/json"
                ))

            client_id = existing_client.Id
            unique_id = existing_client.UniqueId
            first_name = first_name or existing_client.FirstName or ""
            middle_name = middle_name or existing_client.MiddleName or ""
            last_name = last_name or existing_client.LastName or ""
            email = email or existing_client.Email or ""

            cursor.execute("""
                UPDATE Clients
                SET
                    FirstName = ?,
                    MiddleName = ?,
                    LastName = ?,
                    Email = ?,
                    Phone = ?,
                    LeadType = ?,
                    Source = ?,
                    ClassificationType = ?,
                    BorrowerType = ?,
                    Objective = ?,
                    LoanType = ?,
                    Purpose = ?,
                    TransactionType = ?,
                    WithBorrowersGuarantors = ?,
                    AnticipatedSettlementDate = ?,
                    VedaIssues = ?,
                    ConductIssues = ?,
                    ClientNeedsObjectives = ?,
                    ApplicantBackground = ?,
                    ExplanationOfIncome = ?,
                    Security = ?,
                    LoanAmount = ?,
                    SecurityValue = ?,
                    Lvr = ?,
                    SpecialNotes = ?,
                    Status = ?,
                    ReferrerFirstName = ?,
                    ReferrerMiddleName = ?,
                    ReferrerLastName = ?,
                    ReferrerPhone = ?,
                    ReferrerEmail = ?
                WHERE Id = ?
            """, (
                first_name,
                middle_name,
                last_name,
                email,
                phone,
                lead_type,
                source,
                classification_type,
                borrower_type,
                objective,
                loan_type,
                purpose,
                transaction_type,
                with_borrowers_guarantors,
                anticipated_settlement_date,
                veda_issues,
                conduct_issues,
                client_needs_objectives,
                applicant_background,
                explanation_of_income,
                security,
                loan_amount,
                security_value,
                lvr,
                special_notes,
                "Pending Team Call",
                referrer_first_name,
                referrer_middle_name,
                referrer_last_name,
                referrer_phone,
                referrer_email,
                client_id,
            ))

        else:
            unique_id = f"CL-{uuid.uuid4().hex[:8].upper()}"

            cursor.execute("""
                INSERT INTO Clients (
                    UniqueId,
                    FirstName,
                    MiddleName,
                    LastName,
                    Email,
                    Phone,
                    LeadType,
                    Source,
                    ClassificationType,
                    BorrowerType,
                    Objective,
                    LoanType,
                    Purpose,
                    TransactionType,
                    WithBorrowersGuarantors,
                    AnticipatedSettlementDate,
                    VedaIssues,
                    ConductIssues,
                    ClientNeedsObjectives,
                    ApplicantBackground,
                    ExplanationOfIncome,
                    Security,
                    LoanAmount,
                    SecurityValue,
                    Lvr,
                    SpecialNotes,
                    Status,
                    ReferrerFirstName,
                    ReferrerMiddleName,
                    ReferrerLastName,
                    ReferrerPhone,
                    ReferrerEmail,
                    PasswordHash,
                    MustChangePassword,
                    PasswordChangedDate,
                    DocumentType,
                    FileName,
                    FileUrl
                )
                OUTPUT INSERTED.Id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                unique_id,
                first_name,
                middle_name,
                last_name,
                email,
                phone,
                lead_type,
                source,
                classification_type,
                borrower_type,
                objective,
                loan_type,
                purpose,
                transaction_type,
                with_borrowers_guarantors,
                anticipated_settlement_date,
                veda_issues,
                conduct_issues,
                client_needs_objectives,
                applicant_background,
                explanation_of_income,
                security,
                loan_amount,
                security_value,
                lvr,
                special_notes,
                "Pending Team Call",
                referrer_first_name,
                referrer_middle_name,
                referrer_last_name,
                referrer_phone,
                referrer_email,
                None,
                1,
                None,
                document_type,
                uploaded_filename,
                blob_url,
            ))

            client_id = cursor.fetchone()[0]

        if co_borrowers_were_supplied:
            replace_client_co_borrowers(cursor, client_id, co_borrowers)
        else:
            co_borrowers = get_client_co_borrowers(cursor, client_id)

        if uploaded_file:
            storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
            container_name = os.getenv("BLOB_CONTAINER_NAME", "client-files")

            blob_service = BlobServiceClient.from_connection_string(
                storage_connection_string
            )
            container_client = blob_service.get_container_client(container_name)

            safe_filename = uploaded_filename.replace(" ", "_")
            blob_name = f"{unique_id}/{uuid.uuid4().hex}-{safe_filename}"

            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(uploaded_file.stream.read(), overwrite=True)

            blob_url = blob_client.url

            # Re-upload flow:
            # If the client uploads a new file for the same document type after rejection,
            # remove the old rejected document row so the portal shows only the new Pending file.
            cursor.execute("""
                SELECT
                    Id,
                    BlobUrl
                FROM Documents
                WHERE ClientId = ?
                  AND LOWER(DocumentType) = LOWER(?)
                  AND LOWER(COALESCE(Status, 'Pending')) IN ('rejected', 'declined', 'failed')
            """, (
                client_id,
                document_type,
            ))

            rejected_documents = cursor.fetchall()

            for rejected_document in rejected_documents:
                rejected_blob_url = rejected_document.BlobUrl

                if rejected_blob_url:
                    try:
                        parsed_rejected_url = urlparse(rejected_blob_url)
                        rejected_path_parts = parsed_rejected_url.path.lstrip("/").split("/", 1)

                        if len(rejected_path_parts) == 2:
                            rejected_blob_name = unquote(rejected_path_parts[1])
                            container_client.delete_blob(rejected_blob_name)
                    except Exception:
                        logging.exception(
                            "Failed to delete old rejected blob for document %s.",
                            rejected_document.Id,
                        )

            cursor.execute("""
                DELETE FROM Documents
                WHERE ClientId = ?
                  AND LOWER(DocumentType) = LOWER(?)
                  AND LOWER(COALESCE(Status, 'Pending')) IN ('rejected', 'declined', 'failed')
            """, (
                client_id,
                document_type,
            ))

            cursor.execute("""
                INSERT INTO Documents (
                    ClientId,
                    DocumentType,
                    FileName,
                    BlobUrl,
                    Status,
                    VerifiedBy,
                    VerifiedDate,
                    Remarks
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
            """, (
                client_id,
                document_type,
                uploaded_filename,
                blob_url,
                "Pending",
            ))

            cursor.execute("""
                UPDATE Clients
                SET
                    DocumentType = ?,
                    FileName = ?,
                    FileUrl = ?
                WHERE Id = ?
            """, (
                document_type,
                uploaded_filename,
                blob_url,
                client_id
            ))

        document_status = update_client_workflow_status(cursor, client_id)

        conn.commit()
        cursor.close()
        conn.close()

        ghl_sync = sync_client_to_ghl(
            unique_id=unique_id,
            first_name=first_name,
            middle_name=middle_name,
            last_name=last_name,
            email=email,
            phone=phone,
            lead_type=lead_type,
            source=source,
            classification_type=classification_type,
            borrower_type=borrower_type,
            objective=objective,
            loan_type=loan_type,
            purpose=purpose,
            transaction_type=transaction_type,
            with_borrowers_guarantors=with_borrowers_guarantors,
            anticipated_settlement_date=anticipated_settlement_date,
            referrer_first_name=referrer_first_name,
            referrer_middle_name=referrer_middle_name,
            referrer_last_name=referrer_last_name,
            referrer_phone=referrer_phone,
            referrer_email=referrer_email,
            uploaded_documents=document_status["uploadedDocuments"],
            missing_documents=document_status["missingDocuments"],
        )

        ghl_contact_id = extract_ghl_contact_id(ghl_sync)
        ghl_submission_trigger = {
            "success": False,
            "skipped": True,
            "message": "Not an initial submission, GHL sync failed, or GHL contact was not resolved.",
        }

        if (
            is_initial_submission
            and isinstance(ghl_sync, dict)
            and ghl_sync.get("success")
            and ghl_contact_id
        ):
            ghl_submission_trigger = start_submission_workflow(
                ghl_contact_id,
            )

        try:
            ghl_location_id = os.getenv("GHL_LOCATION_ID", "").strip()

            conn = get_sql_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE Clients
                SET
                    GHLContactId = COALESCE(NULLIF(?, ''), GHLContactId),
                    GHLLocationId = COALESCE(NULLIF(?, ''), GHLLocationId),
                    CreatedInGHL = ?,
                    Status = ?
                WHERE Id = ?
            """, (
                ghl_contact_id,
                ghl_location_id,
                bool(ghl_sync.get("success")) if isinstance(ghl_sync, dict) else False,
                "Pending Team Call",
                client_id,
            ))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception:
            logging.exception("Failed to update GHL sync metadata on client record.")

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Client application submitted successfully.",
                "clientId": client_id,
                "uniqueId": unique_id,
                "blobUrl": blob_url,
                "leadType": format_lead_type(lead_type),
                "source": format_lead_type(source),
                "status": "Pending Team Call",
                "intake": {
                    "classificationType": classification_type,
                    "borrowerType": borrower_type,
                    "objective": objective,
                    "loanType": loan_type,
                    "purpose": purpose,
                    "transactionType": transaction_type,
                    "withBorrowersGuarantors": with_borrowers_guarantors,
                    "coBorrowers": co_borrowers,
                    "anticipatedSettlementDate": anticipated_settlement_date,
                    "vedaIssues": veda_issues,
                    "conductIssues": conduct_issues,
                    "clientNeedsObjectives": client_needs_objectives,
                    "applicantBackground": applicant_background,
                    "explanationOfIncome": explanation_of_income,
                    "security": security,
                    "loanAmount": loan_amount,
                    "securityValue": security_value,
                    "lvr": lvr,
                    "specialNotes": special_notes,
                    "referrer": {
                        "firstName": referrer_first_name,
                        "middleName": referrer_middle_name,
                        "lastName": referrer_last_name,
                        "phone": referrer_phone,
                        "email": referrer_email,
                    },
                },
                "documentStatus": {
                    "transactionType": transaction_type,
                    "requiredDocuments": [
                        format_document_type(item)
                        for item in document_status["requiredDocuments"]
                    ],
                    "requiredDocumentsText": format_required_documents_for_ghl(
                        transaction_type
                    ),
                    "uploadedDocuments": [
                        format_document_type(item)
                        for item in document_status["uploadedDocuments"]
                    ],
                    "waivedDocuments": [
                        format_document_type(item)
                        for item in document_status["waivedDocuments"]
                    ],
                    "missingDocuments": [
                        format_document_type(item)
                        for item in document_status["missingDocuments"]
                    ],
                    "isComplete": document_status["isComplete"],
                },
                "ghlSync": ghl_sync,
                "ghlSubmissionTrigger": ghl_submission_trigger,
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Upload failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e)
            }),
            status_code=500,
            mimetype="application/json"
        ))


@app.route(route="documents", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "OPTIONS"])
def get_documents(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        client_id = req.params.get("clientId")
        unique_id = req.params.get("uniqueId")
        status = req.params.get("status")

        conn = get_sql_connection()
        cursor = conn.cursor()

        query = """
            SELECT
                d.Id,
                d.ClientId,
                c.UniqueId,
                c.FirstName,
                c.MiddleName,
                c.LastName,
                c.Email,
                d.DocumentType,
                d.FileName,
                d.BlobUrl,
                d.UploadedAt,
                COALESCE(d.Status, 'Pending') AS Status,
                d.VerifiedBy,
                d.VerifiedDate,
                d.Remarks
            FROM Documents d
            INNER JOIN Clients c ON c.Id = d.ClientId
            WHERE 1 = 1
        """

        params = []

        if client_id:
            query += " AND d.ClientId = ?"
            params.append(client_id)

        if unique_id:
            query += " AND c.UniqueId = ?"
            params.append(unique_id)

        if status:
            query += " AND COALESCE(d.Status, 'Pending') = ?"
            params.append(status)

        query += " ORDER BY d.UploadedAt DESC, d.Id DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        documents = []
        for row in rows:
            documents.append({
                "id": row.Id,
                "clientId": row.ClientId,
                "uniqueId": row.UniqueId,
                "clientName": " ".join(filter(None, [
                    row.FirstName,
                    row.MiddleName,
                    row.LastName,
                ])),
                "email": row.Email,
                "documentType": row.DocumentType,
                "documentLabel": format_document_type(row.DocumentType),
                "fileName": row.FileName,
                "fileUrl": row.BlobUrl,
                "uploadedAt": str(row.UploadedAt) if row.UploadedAt else None,
                "status": row.Status,
                "verifiedBy": row.VerifiedBy,
                "verifiedDate": str(row.VerifiedDate) if row.VerifiedDate else None,
                "remarks": row.Remarks,
            })

        cursor.close()
        conn.close()

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "documents": documents,
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Fetch documents failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e),
            }),
            status_code=500,
            mimetype="application/json"
        ))


def update_document_review_status(document_id, new_status, req: func.HttpRequest):
    try:
        data = {}
        try:
            data = req.get_json()
        except Exception:
            data = {}

        verified_by = clean_value(data.get("verifiedBy") or data.get("adminName") or "Admin")
        remarks = clean_value(data.get("remarks"))

        conn = get_sql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                d.Id,
                d.ClientId,
                d.DocumentType,
                d.FileName,
                c.UniqueId,
                c.FirstName,
                c.MiddleName,
                c.LastName,
                c.Email,
                c.Phone,
                c.LeadType,
                c.Source,
                c.ClassificationType,
                c.BorrowerType,
                c.Objective,
                c.LoanType,
                c.Purpose,
                c.TransactionType,
                c.WithBorrowersGuarantors,
                c.AnticipatedSettlementDate,
                c.ReferrerFirstName,
                c.ReferrerMiddleName,
                c.ReferrerLastName,
                c.ReferrerPhone,
                c.ReferrerEmail
            FROM Documents d
            INNER JOIN Clients c ON c.Id = d.ClientId
            WHERE d.Id = ?
        """, document_id)

        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()

            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Document not found.",
                }),
                status_code=404,
                mimetype="application/json"
            ))

        verified_date = datetime.utcnow() if new_status == "Verified" else None

        cursor.execute("""
            UPDATE Documents
            SET
                Status = ?,
                VerifiedBy = ?,
                VerifiedDate = ?,
                Remarks = ?
            WHERE Id = ?
        """, (
            new_status,
            verified_by,
            verified_date,
            remarks,
            document_id,
        ))

        document_status = update_client_workflow_status(cursor, row.ClientId)

        conn.commit()
        cursor.close()
        conn.close()

        try:
            sync_client_to_ghl(
                unique_id=row.UniqueId,
                first_name=row.FirstName,
                middle_name=row.MiddleName,
                last_name=row.LastName,
                email=row.Email,
                phone=row.Phone,
                lead_type=row.LeadType,
                source=row.Source,
                classification_type=row.ClassificationType,
                borrower_type=row.BorrowerType,
                objective=row.Objective,
                loan_type=row.LoanType,
                purpose=row.Purpose,
                transaction_type=row.TransactionType,
                with_borrowers_guarantors=row.WithBorrowersGuarantors,
                anticipated_settlement_date=str(row.AnticipatedSettlementDate) if row.AnticipatedSettlementDate else "",
                referrer_first_name=row.ReferrerFirstName,
                referrer_middle_name=row.ReferrerMiddleName,
                referrer_last_name=row.ReferrerLastName,
                referrer_phone=row.ReferrerPhone,
                referrer_email=row.ReferrerEmail,
                uploaded_documents=document_status["uploadedDocuments"],
                missing_documents=document_status["missingDocuments"],
            )
        except Exception:
            logging.exception("GHL sync after document review failed.")

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": f"Document marked as {new_status}.",
                "document": {
                    "id": row.Id,
                    "clientId": row.ClientId,
                    "uniqueId": row.UniqueId,
                    "documentType": row.DocumentType,
                    "documentLabel": format_document_type(row.DocumentType),
                    "fileName": row.FileName,
                    "status": new_status,
                    "verifiedBy": verified_by,
                    "verifiedDate": verified_date.isoformat() if verified_date else None,
                    "remarks": remarks,
                },
                "documentStatus": {
                    "requiredDocuments": [
                        format_document_type(item)
                        for item in document_status["requiredDocuments"]
                    ],
                    "uploadedDocuments": [
                        format_document_type(item)
                        for item in document_status["uploadedDocuments"]
                    ],
                    "verifiedDocuments": [
                        format_document_type(item)
                        for item in document_status["verifiedDocuments"]
                    ],
                    "waivedDocuments": [
                        format_document_type(item)
                        for item in document_status["waivedDocuments"]
                    ],
                    "missingDocuments": [
                        format_document_type(item)
                        for item in document_status["missingDocuments"]
                    ],
                    "unverifiedDocuments": [
                        format_document_type(item)
                        for item in document_status["unverifiedDocuments"]
                    ],
                    "isComplete": document_status["isComplete"],
                    "progress": document_status["progress"],
                },
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Update document review status failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e),
            }),
            status_code=500,
            mimetype="application/json"
        ))


@app.route(route="documents/{document_id}/verify", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "PUT", "OPTIONS"])
def verify_document(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    return update_document_review_status(req.route_params.get("document_id"), "Verified", req)


@app.route(route="documents/{document_id}/reject", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "PUT", "OPTIONS"])
def reject_document(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    return update_document_review_status(req.route_params.get("document_id"), "Rejected", req)


@app.route(route="documents/{document_id}/pending", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "PUT", "OPTIONS"])
def pending_document(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    return update_document_review_status(req.route_params.get("document_id"), "Pending", req)


@app.route(
    route="clients/{client_id}/documents/{document_type}/waive",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=["POST", "DELETE", "OPTIONS"],
)
def waive_client_document(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    conn = None
    cursor = None

    try:
        client_id_text = clean_value(req.route_params.get("client_id"))
        document_type = normalize_document_type(
            req.route_params.get("document_type")
        )

        if not client_id_text.isdigit() or not document_type:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "A valid client ID and document type are required.",
                }),
                status_code=400,
                mimetype="application/json",
            ))

        client_id = int(client_id_text)
        data = {}

        try:
            data = req.get_json() or {}
        except ValueError:
            data = {}

        waived_by = clean_value(
            data.get("waivedBy") or data.get("adminName") or "Admin"
        )
        remarks = clean_value(data.get("remarks"))

        conn = get_sql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT TOP 1 Id, TransactionType
            FROM Clients
            WHERE Id = ?
        """, client_id)
        client = cursor.fetchone()

        if not client:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Client not found.",
                }),
                status_code=404,
                mimetype="application/json",
            ))

        required_documents = get_required_documents(client.TransactionType)

        if document_type not in required_documents:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "This document is not required for the client.",
                    "documentType": document_type,
                }),
                status_code=400,
                mimetype="application/json",
            ))

        if req.method == "DELETE":
            cursor.execute("""
                DELETE FROM dbo.DocumentWaivers
                WHERE ClientId = ? AND DocumentType = ?
            """, client_id, document_type)
            action_message = "Document waiver removed."
        else:
            cursor.execute("""
                MERGE dbo.DocumentWaivers AS target
                USING (
                    SELECT ? AS ClientId, ? AS DocumentType
                ) AS source
                ON target.ClientId = source.ClientId
                   AND target.DocumentType = source.DocumentType
                WHEN MATCHED THEN
                    UPDATE SET
                        WaivedBy = ?,
                        Remarks = ?,
                        WaivedAt = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN
                    INSERT (ClientId, DocumentType, WaivedBy, Remarks, WaivedAt)
                    VALUES (?, ?, ?, ?, SYSUTCDATETIME());
            """, (
                client_id,
                document_type,
                waived_by,
                remarks,
                client_id,
                document_type,
                waived_by,
                remarks,
            ))
            action_message = "Document requirement waived."

        document_status = update_client_workflow_status(cursor, client_id)
        conn.commit()

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": action_message,
                "clientId": client_id,
                "documentType": document_type,
                "documentLabel": format_document_type(document_type),
                "waivedBy": waived_by if req.method != "DELETE" else None,
                "remarks": remarks if req.method != "DELETE" else None,
                "documentStatus": document_status,
            }),
            status_code=200,
            mimetype="application/json",
        ))

    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        logging.exception("Document waiver update failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(exc),
            }),
            status_code=500,
            mimetype="application/json",
        ))

    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route(
    route="clients/{client_id}/admin-reference-documents/{document_type}",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=["GET", "POST", "DELETE", "OPTIONS"],
)
def admin_reference_document(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    conn = None
    cursor = None
    new_blob_client = None
    database_committed = False

    try:
        client_id_text = clean_value(req.route_params.get("client_id"))
        document_type = normalize_document_type(
            req.route_params.get("document_type")
        )

        if not client_id_text.isdigit() or not document_type:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "A valid client ID and document type are required.",
                }),
                status_code=400,
                mimetype="application/json",
            ))

        client_id = int(client_id_text)
        conn = get_sql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT TOP 1 Id, TransactionType
            FROM dbo.Clients
            WHERE Id = ?
        """, client_id)
        client = cursor.fetchone()

        if not client:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Client not found.",
                }),
                status_code=404,
                mimetype="application/json",
            ))

        if not is_valid_document_type(client.TransactionType, document_type):
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "This document type is not valid for the client.",
                    "documentType": document_type,
                    "allowedDocumentTypes": get_required_documents(
                        client.TransactionType
                    ),
                }),
                status_code=400,
                mimetype="application/json",
            ))

        cursor.execute("""
            SELECT TOP 1
                Id,
                ClientId,
                DocumentType,
                FileName,
                BlobUrl,
                UploadedBy,
                UploadedAt
            FROM dbo.AdminReferenceDocuments
            WHERE ClientId = ? AND DocumentType = ?
            ORDER BY UploadedAt DESC, Id DESC
        """, client_id, document_type)
        existing_reference = cursor.fetchone()

        if req.method == "GET":
            reference_document = None

            if existing_reference:
                reference_document = {
                    "id": existing_reference.Id,
                    "clientId": existing_reference.ClientId,
                    "documentType": normalize_document_type(
                        existing_reference.DocumentType
                    ),
                    "documentLabel": format_document_type(
                        existing_reference.DocumentType
                    ),
                    "fileName": existing_reference.FileName,
                    "blobUrl": existing_reference.BlobUrl,
                    "uploadedBy": existing_reference.UploadedBy,
                    "uploadedAt": (
                        existing_reference.UploadedAt.isoformat()
                        if existing_reference.UploadedAt
                        else None
                    ),
                }

            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": True,
                    "clientId": client_id,
                    "documentType": document_type,
                    "referenceDocument": reference_document,
                }),
                status_code=200,
                mimetype="application/json",
            ))

        storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
        container_name = os.getenv("BLOB_CONTAINER_NAME", "client-files")

        if not storage_connection_string:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Azure Blob Storage is not configured.",
                }),
                status_code=500,
                mimetype="application/json",
            ))

        blob_service = BlobServiceClient.from_connection_string(
            storage_connection_string
        )
        container_client = blob_service.get_container_client(container_name)

        if req.method == "DELETE":
            if not existing_reference:
                return add_cors(func.HttpResponse(
                    json.dumps({
                        "success": True,
                        "message": "No admin reference document was found.",
                        "clientId": client_id,
                        "documentType": document_type,
                    }),
                    status_code=200,
                    mimetype="application/json",
                ))

            cursor.execute("""
                DELETE FROM dbo.AdminReferenceDocuments
                WHERE Id = ?
            """, existing_reference.Id)
            conn.commit()
            database_committed = True

            try:
                parsed_url = urlparse(existing_reference.BlobUrl)
                path_parts = parsed_url.path.lstrip("/").split("/", 1)

                if len(path_parts) == 2:
                    container_client.delete_blob(unquote(path_parts[1]))
            except Exception:
                logging.exception(
                    "Failed to delete admin reference blob %s.",
                    existing_reference.Id,
                )

            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": True,
                    "message": "Admin reference document removed.",
                    "clientId": client_id,
                    "documentType": document_type,
                }),
                status_code=200,
                mimetype="application/json",
            ))

        uploaded_file = req.files.get("file")

        if not uploaded_file or not clean_value(uploaded_file.filename):
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Select an admin reference file to upload.",
                }),
                status_code=400,
                mimetype="application/json",
            ))

        uploaded_filename = clean_value(uploaded_file.filename)
        allowed_extensions = {
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp",
            ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        }
        file_extension = os.path.splitext(uploaded_filename.lower())[1]

        if file_extension not in allowed_extensions:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Unsupported admin reference file type.",
                    "allowedExtensions": sorted(allowed_extensions),
                }),
                status_code=400,
                mimetype="application/json",
            ))

        uploaded_by = clean_value(
            req.form.get("uploadedBy") or req.form.get("adminName") or "Admin"
        )
        safe_filename = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            uploaded_filename,
        ).strip("._") or f"reference{file_extension}"
        blob_name = (
            f"admin-references/{client_id}/{document_type}/"
            f"{uuid.uuid4().hex}-{safe_filename}"
        )
        new_blob_client = container_client.get_blob_client(blob_name)
        new_blob_client.upload_blob(uploaded_file.stream.read(), overwrite=True)
        new_blob_url = new_blob_client.url

        if existing_reference:
            cursor.execute("""
                UPDATE dbo.AdminReferenceDocuments
                SET
                    FileName = ?,
                    BlobUrl = ?,
                    UploadedBy = ?,
                    UploadedAt = SYSUTCDATETIME()
                WHERE Id = ?
            """, (
                uploaded_filename,
                new_blob_url,
                uploaded_by,
                existing_reference.Id,
            ))
            reference_id = existing_reference.Id
        else:
            cursor.execute("""
                INSERT INTO dbo.AdminReferenceDocuments (
                    ClientId,
                    DocumentType,
                    FileName,
                    BlobUrl,
                    UploadedBy,
                    UploadedAt
                )
                OUTPUT INSERTED.Id
                VALUES (?, ?, ?, ?, ?, SYSUTCDATETIME())
            """, (
                client_id,
                document_type,
                uploaded_filename,
                new_blob_url,
                uploaded_by,
            ))
            reference_id = cursor.fetchone()[0]

        conn.commit()
        database_committed = True

        if existing_reference and existing_reference.BlobUrl:
            try:
                parsed_old_url = urlparse(existing_reference.BlobUrl)
                old_path_parts = parsed_old_url.path.lstrip("/").split("/", 1)

                if len(old_path_parts) == 2:
                    container_client.delete_blob(unquote(old_path_parts[1]))
            except Exception:
                logging.exception(
                    "Failed to delete replaced admin reference blob %s.",
                    existing_reference.Id,
                )

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": (
                    "Admin reference document replaced."
                    if existing_reference
                    else "Admin reference document uploaded."
                ),
                "referenceDocument": {
                    "id": reference_id,
                    "clientId": client_id,
                    "documentType": document_type,
                    "documentLabel": format_document_type(document_type),
                    "fileName": uploaded_filename,
                    "blobUrl": new_blob_url,
                    "uploadedBy": uploaded_by,
                    "uploadedAt": datetime.utcnow().isoformat(),
                },
            }),
            status_code=200,
            mimetype="application/json",
        ))

    except Exception as exc:
        if conn and not database_committed:
            try:
                conn.rollback()
            except Exception:
                pass

        if new_blob_client is not None and not database_committed:
            try:
                new_blob_client.delete_blob()
            except Exception:
                logging.exception(
                    "Failed to clean up an uncommitted admin reference blob."
                )

        logging.exception("Admin reference document request failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(exc),
            }),
            status_code=500,
            mimetype="application/json",
        ))

    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route(
    route="documents/{document_id}/compare",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=["POST", "OPTIONS"],
)
def compare_document_with_admin_reference(
    req: func.HttpRequest,
) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    conn = None
    cursor = None

    try:
        document_id_text = clean_value(req.route_params.get("document_id"))

        if not document_id_text.isdigit():
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "A valid client document ID is required.",
                }),
                status_code=400,
                mimetype="application/json",
            ))

        if not document_intelligence_is_configured():
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Azure Document Intelligence is not configured in this Function App.",
                }),
                status_code=503,
                mimetype="application/json",
            ))

        document_id = int(document_id_text)
        data = {}

        try:
            data = req.get_json() or {}
        except ValueError:
            data = {}

        compared_by = clean_value(
            data.get("comparedBy") or data.get("adminName") or "AI"
        )
        force_comparison = str(data.get("force") or "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        conn = get_sql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT TOP 1
                Id,
                ClientId,
                DocumentType,
                FileName,
                BlobUrl
            FROM dbo.Documents
            WHERE Id = ?
        """, document_id)
        document = cursor.fetchone()

        if not document:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Client document not found.",
                }),
                status_code=404,
                mimetype="application/json",
            ))

        document_type = normalize_document_type(document.DocumentType)
        cursor.execute("""
            SELECT TOP 1
                Id,
                ClientId,
                DocumentType,
                FileName,
                BlobUrl
            FROM dbo.AdminReferenceDocuments
            WHERE ClientId = ? AND DocumentType = ?
            ORDER BY UploadedAt DESC, Id DESC
        """, document.ClientId, document_type)
        reference = cursor.fetchone()

        if not reference:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Upload an admin reference document before running AI comparison.",
                    "clientId": document.ClientId,
                    "documentId": document_id,
                    "documentType": document_type,
                }),
                status_code=409,
                mimetype="application/json",
            ))

        supported_extensions = {
            ".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff",
            ".heif", ".docx", ".xlsx", ".pptx", ".html",
        }
        client_extension = os.path.splitext(
            clean_value(document.FileName).lower()
        )[1]
        reference_extension = os.path.splitext(
            clean_value(reference.FileName).lower()
        )[1]

        if (
            client_extension not in supported_extensions
            or reference_extension not in supported_extensions
        ):
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": (
                        "AI comparison supports PDF, JPEG, PNG, BMP, TIFF, HEIF, "
                        "DOCX, XLSX, PPTX, and HTML files. Convert the unsupported "
                        "file and upload it again."
                    ),
                }),
                status_code=415,
                mimetype="application/json",
            ))

        cursor.close()
        cursor = None
        conn.close()
        conn = None

        client_bytes = download_private_blob(document.BlobUrl)
        reference_bytes = download_private_blob(reference.BlobUrl)

        if not client_bytes or not reference_bytes:
            raise ValueError("One of the comparison files is empty.")

        client_hash = hashlib.sha256(client_bytes).hexdigest()
        reference_hash = hashlib.sha256(reference_bytes).hexdigest()

        if not force_comparison:
            conn = get_sql_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT TOP 1
                    Id,
                    Result,
                    Confidence,
                    ExactMatch,
                    TextSimilarity,
                    KeywordScore,
                    LayoutSimilarity,
                    PredictedDocumentType,
                    ClassifierConfidence,
                    ReasonsJson,
                    ComparedBy,
                    ComparedAt
                FROM dbo.DocumentComparisonResults
                WHERE DocumentId = ?
                  AND AdminReferenceDocumentId = ?
                  AND ClientContentHash = ?
                  AND ReferenceContentHash = ?
                ORDER BY ComparedAt DESC, Id DESC
            """, (
                document_id,
                reference.Id,
                client_hash,
                reference_hash,
            ))
            cached = cursor.fetchone()

            if cached:
                try:
                    cached_reasons = json.loads(cached.ReasonsJson or "[]")
                except (ValueError, TypeError):
                    cached_reasons = []

                return add_cors(func.HttpResponse(
                    json.dumps({
                        "success": True,
                        "message": "Existing automatic comparison loaded.",
                        "comparison": {
                            "id": cached.Id,
                            "documentId": document_id,
                            "adminReferenceDocumentId": reference.Id,
                            "clientId": document.ClientId,
                            "documentType": document_type,
                            "documentLabel": format_document_type(document_type),
                            "clientFileName": document.FileName,
                            "referenceFileName": reference.FileName,
                            "result": cached.Result,
                            "confidence": float(cached.Confidence or 0),
                            "confidencePercent": round(float(cached.Confidence or 0) * 100),
                            "exactMatch": bool(cached.ExactMatch),
                            "textSimilarity": float(cached.TextSimilarity or 0),
                            "keywordScore": float(cached.KeywordScore or 0),
                            "layoutSimilarity": float(cached.LayoutSimilarity or 0),
                            "predictedDocumentType": cached.PredictedDocumentType,
                            "classifierConfidence": (
                                float(cached.ClassifierConfidence)
                                if cached.ClassifierConfidence is not None
                                else None
                            ),
                            "matchedKeywords": [],
                            "reasons": cached_reasons,
                            "requiresHumanReview": cached.Result != "Matched",
                            "comparedBy": cached.ComparedBy,
                            "comparedAt": (
                                cached.ComparedAt.isoformat()
                                if cached.ComparedAt
                                else None
                            ),
                            "cached": True,
                        },
                    }),
                    status_code=200,
                    mimetype="application/json",
                ))

            cursor.close()
            cursor = None
            conn.close()
            conn = None

        comparison = build_document_comparison(
            document_type,
            client_bytes,
            reference_bytes,
        )

        conn = get_sql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO dbo.DocumentComparisonResults (
                DocumentId,
                AdminReferenceDocumentId,
                ExpectedDocumentType,
                PredictedDocumentType,
                Result,
                Confidence,
                ExactMatch,
                TextSimilarity,
                KeywordScore,
                LayoutSimilarity,
                ClassifierConfidence,
                ClientContentHash,
                ReferenceContentHash,
                ReasonsJson,
                ComparedBy,
                ComparedAt
            )
            OUTPUT INSERTED.Id, INSERTED.ComparedAt
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, SYSUTCDATETIME())
        """, (
            document_id,
            reference.Id,
            document_type,
            comparison["predictedDocumentType"],
            comparison["result"],
            comparison["confidence"],
            1 if comparison["exactMatch"] else 0,
            comparison["textSimilarity"],
            comparison["keywordScore"],
            comparison["layoutSimilarity"],
            comparison["classifierConfidence"],
            comparison["clientContentHash"],
            comparison["referenceContentHash"],
            json.dumps(comparison["reasons"]),
            compared_by,
        ))
        inserted = cursor.fetchone()
        conn.commit()

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Automatic document comparison completed.",
                "comparison": {
                    "id": inserted.Id,
                    "documentId": document_id,
                    "adminReferenceDocumentId": reference.Id,
                    "clientId": document.ClientId,
                    "documentType": document_type,
                    "documentLabel": format_document_type(document_type),
                    "clientFileName": document.FileName,
                    "referenceFileName": reference.FileName,
                    "result": comparison["result"],
                    "confidence": comparison["confidence"],
                    "confidencePercent": round(comparison["confidence"] * 100),
                    "exactMatch": comparison["exactMatch"],
                    "textSimilarity": comparison["textSimilarity"],
                    "keywordScore": comparison["keywordScore"],
                    "layoutSimilarity": comparison["layoutSimilarity"],
                    "predictedDocumentType": comparison["predictedDocumentType"],
                    "classifierConfidence": comparison["classifierConfidence"],
                    "matchedKeywords": comparison["matchedKeywords"],
                    "reasons": comparison["reasons"],
                    "requiresHumanReview": comparison["result"] != "Matched",
                    "comparedBy": compared_by,
                    "comparedAt": (
                        inserted.ComparedAt.isoformat()
                        if inserted.ComparedAt
                        else datetime.utcnow().isoformat()
                    ),
                },
            }),
            status_code=200,
            mimetype="application/json",
        ))

    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        logging.exception("Automatic document comparison failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(exc),
            }),
            status_code=500,
            mimetype="application/json",
        ))

    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

        if conn:
            try:
                conn.close()
            except Exception:
                pass



@app.route(route="file-preview", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "OPTIONS"])
def preview_file(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        blob_url = req.params.get("blobUrl")

        if not blob_url:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "blobUrl is required."
                }),
                status_code=400,
                mimetype="application/json",
            ))

        storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
        configured_container = os.getenv("BLOB_CONTAINER_NAME", "client-files")

        if not storage_connection_string:
            raise Exception("STORAGE_CONNECTION_STRING is not configured.")

        parsed_url = urlparse(blob_url)
        path_parts = parsed_url.path.lstrip("/").split("/", 1)

        if len(path_parts) < 2:
            raise Exception("Invalid blob URL.")

        container_name = unquote(path_parts[0]) or configured_container
        blob_name = unquote(path_parts[1])

        blob_service = BlobServiceClient.from_connection_string(
            storage_connection_string
        )
        blob_client = blob_service.get_blob_client(
            container=container_name,
            blob=blob_name,
        )

        properties = blob_client.get_blob_properties()
        content_length = int(properties.size or 0)

        max_preview_size = int(
            os.getenv("MAX_INLINE_PREVIEW_BYTES", str(25 * 1024 * 1024))
        )

        if content_length > max_preview_size:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "This file is too large for inline preview. Please download it instead."
                }),
                status_code=413,
                mimetype="application/json",
            ))

        file_bytes = blob_client.download_blob().readall()
        file_name = os.path.basename(blob_name) or "document.pdf"
        content_type = (
            properties.content_settings.content_type
            or "application/octet-stream"
        )

        # PDF previews require an inline disposition and PDF content type.
        if file_name.lower().endswith(".pdf"):
            content_type = "application/pdf"

        response = func.HttpResponse(
            body=file_bytes,
            status_code=200,
            mimetype=content_type,
        )
        response.headers["Content-Disposition"] = (
            f'inline; filename="{file_name.replace(chr(34), "")}"'
        )
        response.headers["Cache-Control"] = "private, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"

        return add_cors(response)

    except Exception as exc:
        logging.exception("Failed to stream file preview.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(exc) or "Failed to preview file."
            }),
            status_code=500,
            mimetype="application/json",
        ))


@app.route(route="file-url", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "OPTIONS"])
def get_file_url(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        blob_url = req.params.get("blobUrl")

        if not blob_url:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "blobUrl is required."
                }),
                status_code=400,
                mimetype="application/json"
            ))

        storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
        configured_container = os.getenv("BLOB_CONTAINER_NAME", "client-files")

        parsed_url = urlparse(blob_url)
        path_parts = parsed_url.path.lstrip("/").split("/", 1)

        if len(path_parts) < 2:
            raise Exception("Invalid blob URL.")

        container_name = unquote(path_parts[0]) or configured_container
        blob_name = unquote(path_parts[1])

        account_name = (
            storage_connection_string
            .split("AccountName=")[1]
            .split(";")[0]
        )

        account_key = (
            storage_connection_string
            .split("AccountKey=")[1]
            .split(";")[0]
        )

        blob_service = BlobServiceClient.from_connection_string(
            storage_connection_string
        )
        blob_client = blob_service.get_blob_client(
            container=container_name,
            blob=blob_name,
        )

        # Use timezone-aware values and allow for small clock differences between
        # the Function worker and Azure Storage.
        sas_now = datetime.now(timezone.utc)
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            start=sas_now - timedelta(minutes=5),
            expiry=sas_now + timedelta(minutes=60),
        )

        # BlobClient.url correctly escapes literal percent signs and spaces in
        # blob names. Building this URL manually can produce an invalid SAS
        # signature for names such as "2ND%20BIKERS%20PRINTING.pdf".
        secure_url = f"{blob_client.url}?{sas_token}"

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "url": secure_url,
                "expiresInMinutes": 60
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Generate secure URL failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e)
            }),
            status_code=500,
            mimetype="application/json"
        ))


@app.route(route="clients", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "OPTIONS"])
def get_clients(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        document_type = req.params.get("documentType")
        search = req.params.get("search")
        unique_id = req.params.get("uniqueId")

        conn = get_sql_connection()
        cursor = conn.cursor()

        query = """
            SELECT
                c.Id AS ClientId,
                c.UniqueId,
                c.FirstName,
                c.MiddleName,
                c.LastName,
                c.Email,
                c.Phone,
                c.LeadType,
                c.Source,
                c.ClassificationType,
                c.BorrowerType,
                c.Objective,
                c.LoanType,
                c.Purpose,
                c.TransactionType,
                c.WithBorrowersGuarantors,
                c.AnticipatedSettlementDate,
                c.VedaIssues,
                c.ConductIssues,
                c.ClientNeedsObjectives,
                c.ApplicantBackground,
                c.ExplanationOfIncome,
                c.Security,
                c.LoanAmount,
                c.SecurityValue,
                c.Lvr,
                c.SpecialNotes,
                c.Status,
                c.ReferrerFirstName,
                c.ReferrerMiddleName,
                c.ReferrerLastName,
                c.ReferrerPhone,
                c.ReferrerEmail,
                d.Id AS DocumentId,
                d.DocumentType,
                d.FileName,
                d.BlobUrl,
                d.UploadedAt,
                COALESCE(d.Status, 'Pending') AS DocumentStatus,
                d.VerifiedBy,
                d.VerifiedDate,
                d.Remarks,
                c.Progress,
                c.CompletedDate,
                c.ReminderSent,
                c.LastReminderDate,
                c.AssignedSpecialist
            FROM Clients c
            LEFT JOIN Documents d ON d.ClientId = c.Id
            WHERE 1 = 1
        """

        params = []

        if document_type:
            query += " AND LOWER(d.DocumentType) = LOWER(?)"
            params.append(normalize_document_type(document_type))

        if unique_id:
            query += " AND c.UniqueId = ?"
            params.append(unique_id)

        if search:
            query += """
                AND (
                    c.UniqueId LIKE ?
                    OR c.FirstName LIKE ?
                    OR c.MiddleName LIKE ?
                    OR c.LastName LIKE ?
                    OR c.Email LIKE ?
                    OR c.Phone LIKE ?
                    OR c.Source LIKE ?
                    OR c.ClassificationType LIKE ?
                    OR c.BorrowerType LIKE ?
                    OR c.Objective LIKE ?
                    OR c.LoanType LIKE ?
                    OR c.Purpose LIKE ?
                    OR c.TransactionType LIKE ?
                    OR c.VedaIssues LIKE ?
                    OR c.ConductIssues LIKE ?
                    OR c.ClientNeedsObjectives LIKE ?
                    OR c.ApplicantBackground LIKE ?
                    OR c.ExplanationOfIncome LIKE ?
                    OR c.Security LIKE ?
                    OR CAST(c.LoanAmount AS NVARCHAR(50)) LIKE ?
                    OR CAST(c.SecurityValue AS NVARCHAR(50)) LIKE ?
                    OR CAST(c.Lvr AS NVARCHAR(50)) LIKE ?
                    OR c.SpecialNotes LIKE ?
                    OR d.FileName LIKE ?
                    OR d.DocumentType LIKE ?
                )
            """
            like = f"%{search}%"
            params.extend([
                like, like, like, like, like,
                like, like, like, like, like,
                like, like, like, like, like,
                like, like, like, like, like,
                like, like, like, like, like,
            ])

        query += " ORDER BY COALESCE(d.UploadedAt, '1900-01-01') DESC, c.Id DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        clients = []
        co_borrowers_by_client_id = {}
        waived_documents_by_client_id = {}

        for row in rows:
            if row.ClientId not in co_borrowers_by_client_id:
                co_borrowers_by_client_id[row.ClientId] = (
                    get_client_co_borrowers(cursor, row.ClientId)
                )

            if row.ClientId not in waived_documents_by_client_id:
                waived_documents_by_client_id[row.ClientId] = (
                    get_client_waived_documents(cursor, row.ClientId)
                )

            clients.append({
                "id": row.DocumentId or row.ClientId,
                "clientId": row.ClientId,
                "uniqueId": row.UniqueId,
                "firstName": row.FirstName,
                "middleName": row.MiddleName,
                "lastName": row.LastName,
                "name": " ".join(filter(None, [
                    row.FirstName,
                    row.MiddleName,
                    row.LastName
                ])),
                "email": row.Email,
                "phone": row.Phone,
                "leadType": format_lead_type(row.LeadType),
                "source": format_lead_type(row.Source),
                "classificationType": row.ClassificationType,
                "borrowerType": row.BorrowerType,
                "objective": row.Objective,
                "loanType": row.LoanType,
                "purpose": row.Purpose,
                "transactionType": row.TransactionType,
                "withBorrowersGuarantors": row.WithBorrowersGuarantors,
                "coBorrowers": co_borrowers_by_client_id[row.ClientId],
                "anticipatedSettlementDate": str(row.AnticipatedSettlementDate) if row.AnticipatedSettlementDate else None,
                "vedaIssues": row.VedaIssues,
                "conductIssues": row.ConductIssues,
                "clientNeedsObjectives": row.ClientNeedsObjectives,
                "applicantBackground": row.ApplicantBackground,
                "explanationOfIncome": row.ExplanationOfIncome,
                "security": row.Security,
                "loanAmount": str(row.LoanAmount) if row.LoanAmount is not None else None,
                "securityValue": str(row.SecurityValue) if row.SecurityValue is not None else None,
                "lvr": str(row.Lvr) if row.Lvr is not None else None,
                "specialNotes": row.SpecialNotes,
                "status": row.Status,
                "referrer": {
                    "firstName": row.ReferrerFirstName,
                    "middleName": row.ReferrerMiddleName,
                    "lastName": row.ReferrerLastName,
                    "phone": row.ReferrerPhone,
                    "email": row.ReferrerEmail,
                },
                "documentType": row.DocumentType,
                "fileName": row.FileName,
                "fileUrl": row.BlobUrl,
                "submittedAt": str(row.UploadedAt) if row.UploadedAt else None,
                "documentStatus": row.DocumentStatus,
                "waivedDocuments": waived_documents_by_client_id[row.ClientId],
                "verifiedBy": row.VerifiedBy,
                "verifiedDate": str(row.VerifiedDate) if row.VerifiedDate else None,
                "remarks": row.Remarks,
                "progress": row.Progress,
                "completedDate": str(row.CompletedDate) if row.CompletedDate else None,
                "reminderSent": bool(row.ReminderSent) if row.ReminderSent is not None else False,
                "lastReminderDate": str(row.LastReminderDate) if row.LastReminderDate else None,
                "assignedSpecialist": row.AssignedSpecialist,
            })

        cursor.close()
        conn.close()

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "clients": clients
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Fetch clients failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e)
            }),
            status_code=500,
            mimetype="application/json"
        ))


@app.route(
    route="client-change-password",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=["POST", "OPTIONS"],
)
def client_change_password(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    conn = None
    cursor = None

    try:
        data = req.get_json()

        unique_id = clean_value(data.get("uniqueId"))
        current_password = data.get("currentPassword") or ""
        new_password = data.get("newPassword") or ""

        if not unique_id or not current_password or not new_password:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": (
                        "Client ID, current password, and new password are required."
                    ),
                }),
                status_code=400,
                mimetype="application/json",
            ))

        password_errors = validate_new_password(new_password)

        if password_errors:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": password_errors[0],
                    "errors": password_errors,
                }),
                status_code=400,
                mimetype="application/json",
            ))

        if hmac.compare_digest(current_password, new_password):
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": (
                        "Your new password must be different from your current password."
                    ),
                }),
                status_code=400,
                mimetype="application/json",
            ))

        conn = get_sql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT TOP 1
                Id,
                UniqueId,
                FirstName,
                LastName,
                Email,
                PasswordHash,
                COALESCE(MustChangePassword, 1) AS MustChangePassword,
                GHLContactId
            FROM Clients
            WHERE UniqueId = ?
        """, unique_id)

        client = cursor.fetchone()

        if not client:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Client account was not found.",
                }),
                status_code=404,
                mimetype="application/json",
            ))

        stored_hash = clean_value(client.PasswordHash)

        if stored_hash:
            current_password_valid = verify_client_password(
                current_password,
                stored_hash,
            )
        else:
            current_password_valid = hmac.compare_digest(
                current_password.casefold(),
                clean_value(client.LastName).casefold(),
            )

        if not current_password_valid:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "The current password is incorrect.",
                }),
                status_code=401,
                mimetype="application/json",
            ))

        password_hash = hash_client_password(new_password)
        changed_at = datetime.utcnow()

        cursor.execute("""
            UPDATE Clients
            SET
                PasswordHash = ?,
                MustChangePassword = 0,
                PasswordChangedDate = ?
            WHERE Id = ?
        """, (
            password_hash,
            changed_at,
            client.Id,
        ))

        conn.commit()

        ghl_contact_id = clean_value(client.GHLContactId)

        if ghl_contact_id:
            email_notification = retrigger_ghl_tag(
                ghl_contact_id,
                GHL_PASSWORD_CHANGED_TAG,
            )
        else:
            email_notification = {
                "success": False,
                "skipped": True,
                "message": "GHL contact ID is not available.",
            }

        logging.info(
            "Password changed for client %s. GHL result: %s",
            client.UniqueId,
            json.dumps(email_notification, default=str)[:1500],
        )

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Password changed successfully.",
                "mustChangePassword": False,
                "passwordChangedDate": changed_at.isoformat() + "Z",
                "emailNotificationSent": bool(
                    isinstance(email_notification, dict)
                    and email_notification.get("success")
                ),
                "emailNotification": email_notification,
                "emailTriggerTag": GHL_PASSWORD_CHANGED_TAG,
            }),
            status_code=200,
            mimetype="application/json",
        ))

    except ValueError:
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": "A valid JSON request body is required.",
            }),
            status_code=400,
            mimetype="application/json",
        ))

    except Exception as exc:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass

        logging.exception("Client password change failed.")

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(exc),
            }),
            status_code=500,
            mimetype="application/json",
        ))

    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route(
    route="client-login",
    auth_level=func.AuthLevel.ANONYMOUS,
    methods=["POST", "OPTIONS"],
)
def client_login(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    conn = None
    cursor = None

    try:
        data = req.get_json()

        unique_id = clean_value(data.get("uniqueId"))
        password = data.get("password") or ""

        if not unique_id or not password:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Client ID and password are required.",
                }),
                status_code=400,
                mimetype="application/json",
            ))

        conn = get_sql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT TOP 1
                Id,
                UniqueId,
                FirstName,
                MiddleName,
                LastName,
                Email,
                Phone,
                LeadType,
                Source,
                ClassificationType,
                BorrowerType,
                Objective,
                LoanType,
                Purpose,
                TransactionType,
                WithBorrowersGuarantors,
                AnticipatedSettlementDate,
                VedaIssues,
                ConductIssues,
                ClientNeedsObjectives,
                ApplicantBackground,
                ExplanationOfIncome,
                Security,
                LoanAmount,
                SecurityValue,
                Lvr,
                SpecialNotes,
                ReferrerFirstName,
                ReferrerMiddleName,
                ReferrerLastName,
                ReferrerPhone,
                ReferrerEmail,
                Progress,
                CompletedDate,
                ReminderSent,
                LastReminderDate,
                AssignedSpecialist,
                PasswordHash,
                COALESCE(MustChangePassword, 1) AS MustChangePassword,
                PasswordChangedDate
            FROM Clients
            WHERE UniqueId = ?
        """, unique_id)

        client = cursor.fetchone()

        if not client:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Invalid Client ID or password.",
                }),
                status_code=401,
                mimetype="application/json",
            ))

        stored_hash = clean_value(client.PasswordHash)

        if stored_hash:
            password_valid = verify_client_password(password, stored_hash)
            must_change_password = bool(client.MustChangePassword)
        else:
            password_valid = hmac.compare_digest(
                password.casefold(),
                clean_value(client.LastName).casefold(),
            )
            must_change_password = True

        if not password_valid:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Invalid Client ID or password.",
                }),
                status_code=401,
                mimetype="application/json",
            ))

        client_co_borrowers = get_client_co_borrowers(cursor, client.Id)
        client_document_status = get_client_document_status(cursor, client.Id)

        client_payload = {
            "id": client.Id,
            "uniqueId": client.UniqueId,
            "firstName": client.FirstName,
            "middleName": client.MiddleName,
            "lastName": client.LastName,
            "email": client.Email,
            "phone": client.Phone,
            "leadType": format_lead_type(client.LeadType),
            "source": format_lead_type(client.Source),
            "classificationType": client.ClassificationType,
            "borrowerType": client.BorrowerType,
            "objective": client.Objective,
            "loanType": client.LoanType,
            "purpose": client.Purpose,
            "transactionType": client.TransactionType,
            "withBorrowersGuarantors": client.WithBorrowersGuarantors,
            "coBorrowers": client_co_borrowers,
            "anticipatedSettlementDate": (
                str(client.AnticipatedSettlementDate)
                if client.AnticipatedSettlementDate
                else None
            ),
            "vedaIssues": client.VedaIssues,
            "conductIssues": client.ConductIssues,
            "clientNeedsObjectives": client.ClientNeedsObjectives,
            "applicantBackground": client.ApplicantBackground,
            "explanationOfIncome": client.ExplanationOfIncome,
            "security": client.Security,
            "loanAmount": (
                str(client.LoanAmount)
                if client.LoanAmount is not None
                else None
            ),
            "securityValue": (
                str(client.SecurityValue)
                if client.SecurityValue is not None
                else None
            ),
            "lvr": str(client.Lvr) if client.Lvr is not None else None,
            "specialNotes": client.SpecialNotes,
            "referrer": {
                "firstName": client.ReferrerFirstName,
                "middleName": client.ReferrerMiddleName,
                "lastName": client.ReferrerLastName,
                "phone": client.ReferrerPhone,
                "email": client.ReferrerEmail,
            },
            "progress": client.Progress,
            "completedDate": (
                str(client.CompletedDate)
                if client.CompletedDate
                else None
            ),
            "reminderSent": (
                bool(client.ReminderSent)
                if client.ReminderSent is not None
                else False
            ),
            "lastReminderDate": (
                str(client.LastReminderDate)
                if client.LastReminderDate
                else None
            ),
            "assignedSpecialist": client.AssignedSpecialist,
            "requiredDocuments": client_document_status["requiredDocuments"],
            "uploadedDocuments": client_document_status["uploadedDocuments"],
            "verifiedDocuments": client_document_status["verifiedDocuments"],
            "waivedDocuments": client_document_status["waivedDocuments"],
            "missingDocuments": client_document_status["missingDocuments"],
            "unverifiedDocuments": client_document_status["unverifiedDocuments"],
            "documentStatus": client_document_status["documentStatus"],
            "documentProgress": client_document_status["progress"],
            "mustChangePassword": must_change_password,
            "passwordChangedDate": (
                str(client.PasswordChangedDate)
                if client.PasswordChangedDate
                else None
            ),
            "name": " ".join(filter(None, [
                client.FirstName,
                client.MiddleName,
                client.LastName,
            ])),
        }

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Client login successful.",
                "mustChangePassword": must_change_password,
                "client": client_payload,
            }),
            status_code=200,
            mimetype="application/json",
        ))

    except ValueError:
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": "A valid JSON request body is required.",
            }),
            status_code=400,
            mimetype="application/json",
        ))

    except Exception as exc:
        logging.exception("Client login failed.")

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(exc),
            }),
            status_code=500,
            mimetype="application/json",
        ))

    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

        if conn:
            try:
                conn.close()
            except Exception:
                pass
