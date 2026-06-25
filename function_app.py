import azure.functions as func
import logging
import os
import json
import uuid
import pyodbc
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
)

app = func.FunctionApp()


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

        conn.commit()
        cursor.close()
        conn.close()

        return add_cors(func.HttpResponse(
            json.dumps({
                "success": True,
                "message": "Client document uploaded successfully.",
                "clientId": client_id,
                "uniqueId": unique_id,
                "blobUrl": blob_url
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