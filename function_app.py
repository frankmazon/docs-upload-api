import azure.functions as func
import logging
import os
import json
import uuid
import pyodbc
from azure.storage.blob import BlobServiceClient

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
            SELECT
                Id,
                Username,
                Role
            FROM Users
            WHERE Username = ?
            AND Password = ?
        """, (
            username,
            password
        ))

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
                json.dumps({"success": False, "message": "No file uploaded."}),
                status_code=400,
                mimetype="application/json"
            ))

        first_name = form.get("firstName", "")
        middle_name = form.get("middleName", "")
        last_name = form.get("lastName", "")
        email = form.get("email", "")
        document_type = form.get("documentType", "")

        unique_id = f"CL-{uuid.uuid4().hex[:8].upper()}"

        storage_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
        container_name = os.getenv("BLOB_CONTAINER_NAME", "client-files")

        blob_service = BlobServiceClient.from_connection_string(storage_connection_string)
        container_client = blob_service.get_container_client(container_name)

        safe_filename = uploaded_file.filename.replace(" ", "_")
        blob_name = f"{unique_id}/{uuid.uuid4().hex}-{safe_filename}"

        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(uploaded_file.stream.read(), overwrite=True)

        blob_url = blob_client.url

        conn = get_sql_connection()
        cursor = conn.cursor()

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
            blob_url
        ))

        client_id = cursor.fetchone()[0]

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
            json.dumps({"success": False, "message": str(e)}),
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
                c.Id,
                c.UniqueId,
                c.FirstName,
                c.MiddleName,
                c.LastName,
                c.Email,
                d.DocumentType,
                d.FileName,
                d.BlobUrl,
                d.UploadedAt
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
                    OR d.FileName LIKE ?
                )
            """
            like = f"%{search}%"
            params.extend([like, like, like, like, like, like])

        query += " ORDER BY c.Id DESC"

        cursor.execute(query, params)
        rows = cursor.fetchall()

        clients = []
        for row in rows:
            clients.append({
                "id": row.Id,
                "uniqueId": row.UniqueId,
                "firstName": row.FirstName,
                "middleName": row.MiddleName,
                "lastName": row.LastName,
                "name": " ".join(filter(None, [row.FirstName, row.MiddleName, row.LastName])),
                "email": row.Email,
                "documentType": row.DocumentType,
                "fileName": row.FileName,
                "fileUrl": row.BlobUrl,
                "submittedAt": str(row.UploadedAt),
            })

        cursor.close()
        conn.close()

        return add_cors(func.HttpResponse(
            json.dumps({"success": True, "clients": clients}),
            status_code=200,
            mimetype="application/json"
        ))

    except Exception as e:
        logging.exception("Fetch clients failed.")
        return add_cors(func.HttpResponse(
            json.dumps({"success": False, "message": str(e)}),
            status_code=500,
            mimetype="application/json"
        ))