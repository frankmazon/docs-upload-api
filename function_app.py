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
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote
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

REQUIRED_DOCUMENTS = [
    "id",
    "property-documents",
    "credit-history",
    "income-documents",
    "other",
]

DOCUMENT_LABELS = {
    "id": "ID",
    "property-documents": "Property Documents",
    "credit-history": "Credit History",
    "income-documents": "Income Documents",
    "other": "Other",
}


GHL_CUSTOM_FIELD_CONFIG = {
    "GHL_CUSTOM_FIELD_CLIENT_ID": ["client_id", "unique_id"],
    "GHL_CUSTOM_FIELD_DOCUMENT_STATUS": ["document_status"],
    "GHL_CUSTOM_FIELD_MISSING_DOCUMENTS": ["missing_documents"],
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


def format_document_type(document_type: str) -> str:
    clean_type = (document_type or "").strip().lower()
    return DOCUMENT_LABELS.get(
        clean_type,
        clean_type.replace("-", " ").title() if clean_type else "Document",
    )


def format_lead_type(lead_type: str) -> str:
    clean_type = (lead_type or "broker").strip().lower()
    return LEAD_TYPE_LABELS.get(clean_type, clean_type.replace("-", " ").title())


def get_client_document_status(cursor, client_id: int):
    cursor.execute("""
        SELECT
            DocumentType,
            COALESCE(Status, 'Pending') AS Status
        FROM Documents
        WHERE ClientId = ?
    """, client_id)

    rows = cursor.fetchall()

    uploaded_raw = [
        (row.DocumentType or "").strip().lower()
        for row in rows
        if row.DocumentType
    ]

    verified_raw = [
        (row.DocumentType or "").strip().lower()
        for row in rows
        if row.DocumentType and (row.Status or "").strip().lower() == "verified"
    ]

    uploaded_documents = sorted(set(uploaded_raw))
    verified_documents = sorted(set(verified_raw))

    missing_documents = [
        document_type
        for document_type in REQUIRED_DOCUMENTS
        if document_type not in uploaded_documents
    ]

    unverified_documents = [
        document_type
        for document_type in REQUIRED_DOCUMENTS
        if document_type in uploaded_documents and document_type not in verified_documents
    ]

    progress = round((len(verified_documents) / len(REQUIRED_DOCUMENTS)) * 100) if REQUIRED_DOCUMENTS else 0
    is_complete = len(missing_documents) == 0 and len(unverified_documents) == 0

    return {
        "uploadedDocuments": uploaded_documents,
        "verifiedDocuments": verified_documents,
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

        document_type = clean_value(form.get("documentType"))
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
                    "uploadedDocuments": [
                        format_document_type(item)
                        for item in document_status["uploadedDocuments"]
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
                    "uploadedDocuments": [
                        format_document_type(item)
                        for item in document_status["uploadedDocuments"]
                    ],
                    "verifiedDocuments": [
                        format_document_type(item)
                        for item in document_status["verifiedDocuments"]
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
        container_name = os.getenv("BLOB_CONTAINER_NAME", "client-files")

        parsed_url = urlparse(blob_url)
        path_parts = parsed_url.path.lstrip("/").split("/", 1)

        if len(path_parts) < 2:
            raise Exception("Invalid blob URL.")

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

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(minutes=30),
        )

        secure_url = (
            f"https://{account_name}.blob.core.windows.net/"
            f"{container_name}/{blob_name}?{sas_token}"
        )

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "url": secure_url,
                "expiresInMinutes": 30
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
            query += " AND d.DocumentType = ?"
            params.append(document_type)

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

        for row in rows:
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