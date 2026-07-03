import azure.functions as func
import logging
import os
import json
import uuid
import pyodbc
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
)

app = func.FunctionApp()

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

LEAD_TYPE_LABELS = {
    "business_owner": "Business Owner",
    "business-owner": "Business Owner",
    "business owner": "Business Owner",
    "referrer": "Referrer",
}


def add_cors(response: func.HttpResponse) -> func.HttpResponse:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
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


def format_document_type(document_type: str) -> str:
    clean_type = (document_type or "").strip().lower()
    return DOCUMENT_LABELS.get(
        clean_type,
        clean_type.replace("-", " ").title() if clean_type else "Document",
    )


def format_lead_type(lead_type: str) -> str:
    clean_type = (lead_type or "business_owner").strip().lower()
    return LEAD_TYPE_LABELS.get(clean_type, "Business Owner")


def get_client_document_status(cursor, client_id: int):
    cursor.execute("""
        SELECT DISTINCT DocumentType
        FROM Documents
        WHERE ClientId = ?
    """, client_id)

    rows = cursor.fetchall()

    uploaded_raw = [
        (row.DocumentType or "").strip().lower()
        for row in rows
        if row.DocumentType
    ]

    uploaded_documents = sorted(set(uploaded_raw))

    missing_documents = [
        document_type
        for document_type in REQUIRED_DOCUMENTS
        if document_type not in uploaded_documents
    ]

    return {
        "uploadedDocuments": uploaded_documents,
        "missingDocuments": missing_documents,
        "isComplete": len(missing_documents) == 0,
    }


def get_ghl_headers():
    token = os.getenv("GHL_ACCESS_TOKEN", "").strip()

    return {
        "Authorization": f"Bearer {token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }


def build_ghl_custom_fields(unique_id, document_status, missing_documents):
    custom_fields = []

    client_id_field = os.getenv("GHL_CUSTOM_FIELD_CLIENT_ID", "").strip()
    status_field = os.getenv("GHL_CUSTOM_FIELD_DOCUMENT_STATUS", "").strip()
    missing_docs_field = os.getenv("GHL_CUSTOM_FIELD_MISSING_DOCUMENTS", "").strip()

    if client_id_field:
        custom_fields.append({
            "id": client_id_field,
            "field_value": unique_id,
        })

    if status_field:
        custom_fields.append({
            "id": status_field,
            "field_value": document_status,
        })

    if missing_docs_field:
        custom_fields.append({
            "id": missing_docs_field,
            "field_value": ", ".join(missing_documents),
        })

    return custom_fields


def sync_client_to_ghl(
    unique_id,
    first_name,
    middle_name,
    last_name,
    email,
    phone,
    lead_type,
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

        full_name = " ".join(
            filter(None, [first_name, middle_name, last_name])
        ).strip()

        tags = [
            "Azure Client Portal",
            "Website Intake",
            lead_label,
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
            "source": "Website Intake",
            "tags": tags,
        }

        custom_fields = build_ghl_custom_fields(
            unique_id=unique_id,
            document_status=document_status,
            missing_documents=missing_labels,
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

        parsed_body = None
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

        if not uploaded_file:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "No file uploaded."
                }),
                status_code=400,
                mimetype="application/json"
            ))

        existing_unique_id = form.get("uniqueId", "").strip()
        first_name = form.get("firstName", "").strip()
        middle_name = form.get("middleName", "").strip()
        last_name = form.get("lastName", "").strip()
        email = form.get("email", "").strip()
        phone = form.get("phone", "").strip()
        lead_type = form.get("leadType", "business_owner").strip().lower()
        document_type = form.get("documentType", "").strip()

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

        else:
            unique_id = f"CL-{uuid.uuid4().hex[:8].upper()}"

            cursor.execute("""
                INSERT INTO Clients (
                    UniqueId,
                    FirstName,
                    MiddleName,
                    LastName,
                    Email,
                    DocumentType,
                    FileName,
                    FileUrl
                )
                OUTPUT INSERTED.Id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                unique_id,
                first_name,
                middle_name,
                last_name,
                email,
                document_type,
                uploaded_file.filename,
                ""
            ))

            client_id = cursor.fetchone()[0]

        storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
        container_name = os.getenv("BLOB_CONTAINER_NAME", "client-files")

        blob_service = BlobServiceClient.from_connection_string(
            storage_connection_string
        )
        container_client = blob_service.get_container_client(container_name)

        safe_filename = uploaded_file.filename.replace(" ", "_")
        blob_name = f"{unique_id}/{uuid.uuid4().hex}-{safe_filename}"

        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(uploaded_file.stream.read(), overwrite=True)

        blob_url = blob_client.url

        cursor.execute("""
            INSERT INTO Documents (
                ClientId,
                DocumentType,
                FileName,
                BlobUrl
            )
            VALUES (?, ?, ?, ?)
        """, (
            client_id,
            document_type,
            uploaded_file.filename,
            blob_url
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
            uploaded_file.filename,
            blob_url,
            client_id
        ))

        document_status = get_client_document_status(cursor, client_id)

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
            uploaded_documents=document_status["uploadedDocuments"],
            missing_documents=document_status["missingDocuments"],
        )

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Client document uploaded successfully.",
                "clientId": client_id,
                "uniqueId": unique_id,
                "blobUrl": blob_url,
                "leadType": format_lead_type(lead_type),
                "status": "Pending Team Call",
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
                d.Id AS DocumentId,
                d.DocumentType,
                d.FileName,
                d.BlobUrl,
                d.UploadedAt
            FROM Clients c
            INNER JOIN Documents d ON d.ClientId = c.Id
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
                    OR d.FileName LIKE ?
                    OR d.DocumentType LIKE ?
                )
            """
            like = f"%{search}%"
            params.extend([like, like, like, like, like, like, like])

        query += " ORDER BY d.UploadedAt DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        clients = []

        for row in rows:
            clients.append({
                "id": row.DocumentId,
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
                "documentType": row.DocumentType,
                "fileName": row.FileName,
                "fileUrl": row.BlobUrl,
                "submittedAt": str(row.UploadedAt),
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


@app.route(route="client-login", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "OPTIONS"])
def client_login(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "OPTIONS":
        return add_cors(func.HttpResponse("", status_code=204))

    try:
        data = req.get_json()

        unique_id = data.get("uniqueId", "").strip()
        password = data.get("password", "").strip()

        if not unique_id or not password:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Client ID and password are required."
                }),
                status_code=400,
                mimetype="application/json"
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
                Email
            FROM Clients
            WHERE UniqueId = ?
            AND LOWER(LastName) = LOWER(?)
        """, (
            unique_id,
            password
        ))

        client = cursor.fetchone()

        cursor.close()
        conn.close()

        if not client:
            return add_cors(func.HttpResponse(
                json.dumps({
                    "success": False,
                    "message": "Invalid Client ID or password."
                }),
                status_code=401,
                mimetype="application/json"
            ))

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Client login successful.",
                "client": {
                    "id": client.Id,
                    "uniqueId": client.UniqueId,
                    "firstName": client.FirstName,
                    "middleName": client.MiddleName,
                    "lastName": client.LastName,
                    "email": client.Email,
                    "name": " ".join(filter(None, [
                        client.FirstName,
                        client.MiddleName,
                        client.LastName
                    ]))
                }
            }),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Client login failed.")
        return add_cors(func.HttpResponse(
            json.dumps({
                "success": False,
                "message": str(e)
            }),
            status_code=500,
            mimetype="application/json"
        ))