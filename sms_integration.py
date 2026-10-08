"""CompleteSMS transport and guarded GHL workflow endpoint. Sending is opt-in."""
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import azure.functions as func
import requests

TEMPLATES = {
    'submission': "Hi {name}, thanks for submitting your scenario to SBR Funding. We've emailed your next steps. Upload your documents here: https://dashboard.sbrfunding.com.au/clients",
    'received': "Hi {name}, thanks for sending through your document(s). We've received them and will review them as part of your initial checks. We'll let you know if anything further is required.",
    'reminder1': "Hi {name}, just a quick reminder that we're still waiting on some outstanding documents for your scenario. Please upload the remaining documents when you can so we can keep things moving. Thank you!",
    'reminder2': "Hi {name}, we're following up regarding the outstanding documents for your scenario. Please upload the remaining documents when you have a moment so we can continue progressing your file. Thanks!",
    'reminder3': "Hi {name}, we're still waiting on the outstanding documents needed to progress with our checks. Please upload the remaining documents at your earliest convenience. If you need any assistance, please let us know.",
    'reminder4': "Hi {name}, just another reminder that we're still waiting on some documents. Please upload the outstanding documents as soon as possible to avoid any delays. If you need help, please contact our team.",
    'reminder5': "Hi {name}, this is a final reminder regarding the outstanding documents for your scenario. Please upload the remaining documents as soon as possible to avoid any delays. If you need assistance, please contact our team. Otherwise, we're happy to place your file on hold until you're ready to proceed. We're ready when you are.",
}


def phone_number(value):
    phone = re.sub(r'[\s().-]', '', str(value or ''))
    if re.fullmatch(r'\+?610[23478]\d{8}', phone):
        phone = '+61' + phone.lstrip('+')[3:]
    if re.fullmatch(r'04\d{8}', phone):
        phone = '+61' + phone[1:]
    elif re.fullmatch(r'61\d{9}', phone):
        phone = '+' + phone
    if not re.fullmatch(r'\+[1-9]\d{7,14}', phone):
        raise ValueError('Client phone must be a valid international number.')
    return phone


def message_text(stage, name):
    name = ' '.join(str(name or 'there').split())[:80]
    return TEMPLATES[stage].format(name=name) + ' - SBR Funding'


def blocked_contact(contact):
    # GHL documents absent global DND as false across its APIs.
    # Explicit null or malformed values remain unverified.
    if contact.get('dnd', False) is not False:
        return True
    sms = (contact.get('dndSettings') or {}).get('SMS') or {}
    if str(sms.get('status', '')).lower() in ('active', 'permanent'):
        return True
    tags = {str(tag).strip().lower() for tag in contact.get('tags', [])}
    return bool(tags & {'sms-opt-out', 'on-hold', 'on hold', 'closed'})


def send_sms(phone, text):
    """Return queued, rejected or unknown; never retry an ambiguous POST."""
    settings = [os.getenv(key, '').strip() for key in (
        'COMPLETESMS_ACCOUNT_ID', 'COMPLETESMS_USERNAME', 'COMPLETESMS_API_PASSWORD')]
    if not all(settings):
        raise ValueError('CompleteSMS credentials are not configured.')
    payload = {
        'Name': 'ROX_API_Request', 'Version': '1.1', 'Product': 'SBR Funding Portal',
        'Sender': dict(zip(('AccountId', 'Username', 'Password'), settings)),
        'Messages': {'ReceiptFlag': 1, 'ReceiptCallbackURL': None, 'ReplyType': 'E',
                     'TimeZoneOffset': 0, 'Count': 1,
                     '1': {'DestTn': phone, 'SMSText': text}},
    }
    try:
        response = requests.post('https://api.completesms.net/smsapi/send',
                                 json=payload, timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200:
            return 'unknown', None
        body = response.json()
        if not isinstance(body, dict):
            return 'unknown', None
        if body.get('Result') != '0000':
            return ('rejected' if body.get('Result') else 'unknown'), None
        item = (body.get('Messages') or {}).get('1', {})
        if item.get('Result') == '0000' and item.get('SMSId') is not None:
            return 'queued', str(item['SMSId'])[:100]
        return ('rejected' if item.get('Result') else 'unknown'), None
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        # Do not log provider exceptions/payloads: they can contain credentials.
        return 'unknown', None


def ensure_table(cursor):
    cursor.execute("""
        DECLARE @lock int;
        EXEC @lock = sp_getapplock @Resource='SbrSmsSchema',
             @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000;
        IF @lock < 0 THROW 51000, 'SMS schema lock unavailable', 1;
        IF OBJECT_ID('dbo.ClientSmsEvents', 'U') IS NULL
        CREATE TABLE dbo.ClientSmsEvents (
            Id bigint IDENTITY PRIMARY KEY,
            ClientId int NOT NULL REFERENCES dbo.Clients(Id),
            Stage varchar(16) NOT NULL,
            EventKey varchar(80) NOT NULL,
            State varchar(16) NOT NULL,
            CreatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
            ProviderId varchar(100) NULL,
            CONSTRAINT UQ_ClientSmsEvent UNIQUE(ClientId, Stage, EventKey)
        );
    """)


def schedule_submission_sms(cursor, client_id):
    """Persist with the contact link transaction; never backfill old submissions."""
    cursor.execute("""
        DECLARE @lock int;
        EXEC @lock = sp_getapplock @Resource='SbrSmsJobsSchema',
             @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000;
        IF @lock < 0 THROW 51000, 'SMS jobs schema lock unavailable', 1;
        IF OBJECT_ID('dbo.ClientSmsJobs', 'U') IS NULL
        CREATE TABLE dbo.ClientSmsJobs (
            ClientId int PRIMARY KEY REFERENCES dbo.Clients(Id) ON DELETE CASCADE,
            State varchar(16) NOT NULL DEFAULT 'pending',
            Attempts int NOT NULL DEFAULT 0,
            AvailableAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
            Result varchar(80) NULL
        );
    """)
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM dbo.ClientSmsJobs WITH (UPDLOCK, HOLDLOCK) WHERE ClientId=?)
        INSERT INTO dbo.ClientSmsJobs(ClientId) VALUES (?)
    """, client_id, client_id)


def process_submission_jobs(connect, handler):
    """Leased jobs survive restarts; provider reservations prevent ambiguous retries."""
    if os.getenv('SMS_SENDING_ENABLED', '').lower() != 'true':
        return
    secret = os.getenv('SMS_WEBHOOK_SECRET', '')
    if not secret:
        return
    for _ in range(5):
        conn = cursor = None
        try:
            conn = connect(query_timeout=15)
            cursor = conn.cursor()
            cursor.execute("SELECT OBJECT_ID('dbo.ClientSmsJobs', 'U')")
            if not cursor.fetchone()[0]:
                return
            cursor.execute("""
                UPDATE dbo.ClientSmsJobs SET State='failed', Result='lease_exhausted'
                WHERE State='processing' AND Attempts >= 3 AND AvailableAt <= SYSUTCDATETIME()
            """)
            cursor.execute("""
                ;WITH next_job AS (
                    SELECT TOP (1) * FROM dbo.ClientSmsJobs WITH (UPDLOCK, READPAST, READCOMMITTEDLOCK)
                    WHERE State IN ('pending', 'processing') AND Attempts < 3
                      AND AvailableAt <= SYSUTCDATETIME()
                    ORDER BY AvailableAt
                )
                UPDATE next_job SET State='processing', Attempts=Attempts+1,
                    AvailableAt=DATEADD(minute,5,SYSUTCDATETIME())
                OUTPUT INSERTED.ClientId, INSERTED.Attempts
            """)
            job = cursor.fetchone()
            if not job:
                conn.commit()
                return
            client_id, attempts = job[0], job[1]
            cursor.execute('SELECT UniqueId FROM dbo.Clients WHERE Id=?', client_id)
            client = cursor.fetchone()
            conn.commit()
            if not client:
                continue
            req = func.HttpRequest(method='POST', url='/api/sms/document-stage',
                headers={'Authorization': 'Bearer ' + secret},
                body=json.dumps({'clientId': client[0], 'stage': 'submission', 'dryRun': False}).encode())
            response = handler(req)
            result = json.loads(response.get_body())
            state = result.get('state', 'error')
            # Only errors before/around a reservation are retried. The handler
            # refuses to resend any existing reservation, including unknown.
            retry = response.status_code >= 500 and attempts < 3
            job_state = 'pending' if retry else ('done' if state in ('queued', 'duplicate', 'skipped') else 'failed')
            cursor.execute("""
                UPDATE dbo.ClientSmsJobs SET State=?, Result=?,
                    AvailableAt=DATEADD(minute,2,SYSUTCDATETIME())
                WHERE ClientId=? AND Attempts=?
            """, job_state, str(result.get('reason', state))[:80], client_id, attempts)
            conn.commit()
            logging.info('Submission SMS job completed: state=%s result=%s', job_state, state)
        except Exception:
            logging.error('Submission SMS job interrupted; durable lease retained.')
            return
        finally:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            for resource in (cursor, conn):
                if resource:
                    try:
                        resource.close()
                    except Exception:
                        pass


def schedule_document_sms(cursor, client_id, stage, document_id=0):
    """Enqueue only explicit new uploads/submissions; never reset an existing job."""
    if stage != 'received' and stage not in {f'reminder{i}' for i in range(1, 6)}:
        raise ValueError('Invalid document SMS stage')
    if stage == 'received' and document_id <= 0:
        raise ValueError('A saved document is required')
    cursor.execute("""
        DECLARE @lock int;
        EXEC @lock = sp_getapplock @Resource='SbrDocumentSmsJobsSchema',
            @LockMode='Exclusive', @LockOwner='Transaction', @LockTimeout=10000;
        IF @lock < 0 THROW 51000, 'Document SMS schema lock unavailable', 1;
        IF OBJECT_ID('dbo.ClientDocumentSmsJobs', 'U') IS NULL
        CREATE TABLE dbo.ClientDocumentSmsJobs (
            Id bigint IDENTITY PRIMARY KEY,
            ClientId int NOT NULL REFERENCES dbo.Clients(Id) ON DELETE CASCADE,
            Stage varchar(16) NOT NULL,
            DocumentId int NOT NULL DEFAULT 0,
            State varchar(16) NOT NULL DEFAULT 'pending',
            Attempts int NOT NULL DEFAULT 0,
            AvailableAt datetime2 NOT NULL,
            Result varchar(80) NULL,
            CONSTRAINT UQ_ClientDocumentSmsJob UNIQUE(ClientId,Stage,DocumentId)
        );
    """)
    if stage == 'received':
        due = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)
    elif stage == 'reminder1':
        cursor.execute('SELECT SubmittedAt FROM dbo.Clients WHERE Id=?', client_id)
        row = cursor.fetchone()
        if not row or not row[0]:
            return
        due = row[0] + timedelta(hours=48)
    else:
        cursor.execute("SELECT CreatedAt FROM dbo.ClientSmsEvents WHERE ClientId=? AND Stage=? AND State='queued'", client_id, f'reminder{int(stage[-1])-1}')
        row = cursor.fetchone()
        if not row:
            return
        due = row[0] + timedelta(hours=48)
    cursor.execute("""
        IF NOT EXISTS (SELECT 1 FROM dbo.ClientDocumentSmsJobs WITH (UPDLOCK,HOLDLOCK)
                       WHERE ClientId=? AND Stage=? AND DocumentId=?)
        INSERT INTO dbo.ClientDocumentSmsJobs(ClientId,Stage,DocumentId,AvailableAt) VALUES(?,?,?,?)
    """, client_id, stage, document_id, client_id, stage, document_id, due)


def document_job_outcome(response, attempts):
    result = json.loads(response.get_body())
    state = result.get('state', 'error')
    accepted = state == 'queued' or (state == 'duplicate' and result.get('previousState') == 'queued')
    retry = attempts < 3 and (response.status_code >= 500 or result.get('reason') in ('missing_ghl_contact', 'not_due'))
    status = 'pending' if retry else ('done' if accepted else ('stopped' if state == 'skipped' else 'failed'))
    return status, str(result.get('reason', state))[:80], accepted


def process_document_jobs(connect, handler):
    if os.getenv('SMS_SENDING_ENABLED', '').lower() != 'true' or not os.getenv('SMS_WEBHOOK_SECRET'):
        return
    for _ in range(5):
        conn = cursor = None
        try:
            conn = connect(query_timeout=15)
            cursor = conn.cursor()
            cursor.execute("SELECT OBJECT_ID('dbo.ClientDocumentSmsJobs','U')")
            if not cursor.fetchone()[0]:
                return
            cursor.execute("""
                UPDATE dbo.ClientDocumentSmsJobs SET State='failed',Result='lease_exhausted'
                WHERE State='processing' AND Attempts>=3 AND AvailableAt<=SYSUTCDATETIME();
            """)
            cursor.execute("""
                ;WITH next_job AS (
                    SELECT TOP (1) * FROM dbo.ClientDocumentSmsJobs WITH (UPDLOCK,READPAST,READCOMMITTEDLOCK)
                    WHERE State IN ('pending','processing') AND Attempts<3 AND AvailableAt<=SYSUTCDATETIME()
                    ORDER BY AvailableAt,Id
                )
                UPDATE next_job SET State='processing',Attempts=Attempts+1,
                    AvailableAt=DATEADD(minute,5,SYSUTCDATETIME())
                OUTPUT INSERTED.Id,INSERTED.ClientId,INSERTED.Stage,INSERTED.DocumentId,INSERTED.Attempts;
            """)
            job = cursor.fetchone()
            if not job:
                conn.commit()
                return
            job_id, client_id, stage, document_id, attempts = job
            cursor.execute('SELECT UniqueId FROM dbo.Clients WHERE Id=?', client_id)
            client = cursor.fetchone()
            conn.commit()
            if not client:
                continue
            req = func.HttpRequest(method='POST',url='/api/sms/document-stage',
                headers={'Authorization':'Bearer '+os.environ['SMS_WEBHOOK_SECRET']},
                body=json.dumps({'clientId':client[0],'stage':stage,'documentId':document_id,'dryRun':False}).encode())
            response = handler(req)
            status, reason, accepted = document_job_outcome(response, attempts)
            # Atomically finish this job and schedule the next reminder. If this
            # transaction fails, the send reservation makes recovery a no-send duplicate.
            if accepted and stage.startswith('reminder') and int(stage[-1]) < 5:
                schedule_document_sms(cursor, client_id, f'reminder{int(stage[-1])+1}')
            cursor.execute("""UPDATE dbo.ClientDocumentSmsJobs SET State=?,Result=?,
                AvailableAt=CASE WHEN ?='pending' THEN DATEADD(minute,5,SYSUTCDATETIME()) ELSE AvailableAt END
                WHERE Id=? AND Attempts=?""", status, reason, status, job_id, attempts)
            conn.commit()
            logging.info('Document SMS job stage=%s state=%s result=%s',stage,status,reason)
        except Exception:
            logging.error('Document SMS job interrupted; durable lease retained.')
            return
        finally:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            for resource in (cursor,conn):
                if resource:
                    try:
                        resource.close()
                    except Exception:
                        pass


def register_sms(app, connect, document_status, ghl_headers):
    @app.route(route='sms/document-stage', methods=['POST'], auth_level=func.AuthLevel.ANONYMOUS)
    def document_sms(req):
        def reply(body, status=200):
            return func.HttpResponse(json.dumps(body), status_code=status,
                                     mimetype='application/json')

        secret = os.getenv('SMS_WEBHOOK_SECRET', '')
        token = req.headers.get('Authorization', '')
        if not secret or not hmac.compare_digest(token, 'Bearer ' + secret):
            return reply({'error': 'Unauthorized'}, 401)
        try:
            data = req.get_json()
            if not isinstance(data, dict):
                raise ValueError()
            stage = data.get('stage')
            client_id = str(data.get('clientId', '')).strip()
            if stage not in TEMPLATES or not re.fullmatch(r'CL-[A-Za-z0-9]+', client_id):
                raise ValueError()
            document_id = int(data.get('documentId', 0)) if stage == 'received' else 0
            if stage == 'received' and document_id <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            return reply({'error': 'Provide clientId, a valid stage, and documentId for received.'}, 400)

        conn = cursor = None
        try:
            conn = connect(query_timeout=15)
            cursor = conn.cursor()
            cursor.execute('SELECT Id, FirstName, Phone, Status, SubmittedAt, GHLContactId FROM dbo.Clients WHERE UniqueId=?', client_id)
            client = cursor.fetchone()
            if not client:
                return reply({'error': 'Client not found'}, 404)
            status = str(client.Status or '').strip().lower()
            if status in ('on hold', 'on-hold', 'closed', 'cancelled', 'canceled', 'declined', 'settled'):
                return reply({'state': 'skipped', 'reason': 'file_inactive'})
            info = document_status(cursor, client.Id)
            if stage.startswith('reminder') and not info['missingDocuments']:
                return reply({'state': 'skipped', 'reason': 'no_outstanding_documents'})
            if stage == 'received':
                cursor.execute('SELECT Id FROM dbo.Documents WHERE Id=? AND ClientId=?', document_id, client.Id)
                if not cursor.fetchone():
                    return reply({'error': 'Document does not belong to client'}, 400)
            phone = phone_number(client.Phone)
            text = message_text(stage, client.FirstName)
            # Finish the read transaction before making a network request.
            conn.commit()
            if not client.GHLContactId:
                return reply({'state': 'blocked', 'reason': 'missing_ghl_contact'}, 409)
            contact_response = requests.get(
                'https://services.leadconnectorhq.com/contacts/' + str(client.GHLContactId),
                headers=ghl_headers(), timeout=(5, 15))
            contact_response.raise_for_status()
            contact = contact_response.json().get('contact', {})
            if contact.get('id') != client.GHLContactId or blocked_contact(contact):
                return reply({'state': 'skipped', 'reason': 'contact_opted_out_or_unverified'})
            # Avoid delivering to a stale number whose consent cannot be checked.
            if not contact.get('phone'):
                return reply({'state': 'blocked', 'reason': 'missing_ghl_phone'}, 409)
            if phone_number(contact.get('phone')) != phone:
                return reply({'state': 'blocked', 'reason': 'phone_mismatch'}, 409)
            enabled = os.getenv('SMS_SENDING_ENABLED', '').lower() == 'true'
            if not enabled or data.get('dryRun') is not False:
                return reply({'state': 'preview', 'stage': stage, 'message': text,
                              'missingDocuments': info['missingDocuments'],
                              'sendingEnabled': enabled})
            if not all(os.getenv(k, '').strip() for k in (
                'COMPLETESMS_ACCOUNT_ID', 'COMPLETESMS_USERNAME', 'COMPLETESMS_API_PASSWORD')):
                return reply({'error': 'SMS credentials not configured'}, 503)
            ensure_table(cursor)
            # Serialize claims across workers and reserve before contacting provider.
            cursor.execute("SELECT Id FROM dbo.Clients WITH (UPDLOCK, HOLDLOCK) WHERE Id=?", client.Id)
            cursor.fetchone()
            event_key = str(document_id) if stage == 'received' else 'initial'
            cursor.execute('SELECT State FROM dbo.ClientSmsEvents WHERE ClientId=? AND Stage=? AND EventKey=?', client.Id, stage, event_key)
            existing = cursor.fetchone()
            if existing:
                return reply({'state': 'duplicate', 'previousState': existing.State})
            if stage.startswith('reminder'):
                # Recheck after acquiring lock; pending-review uploads count as received.
                if not document_status(cursor, client.Id)['missingDocuments']:
                    return reply({'state': 'skipped', 'reason': 'no_outstanding_documents'})
                number = int(stage[-1])
                previous_time = client.SubmittedAt
                if number > 1:
                    cursor.execute("SELECT CreatedAt FROM dbo.ClientSmsEvents WHERE ClientId=? AND Stage=? AND State='queued'", client.Id, f'reminder{number-1}')
                    previous = cursor.fetchone()
                    if not previous:
                        return reply({'state': 'blocked', 'reason': 'previous_reminder_not_queued'}, 409)
                    previous_time = previous.CreatedAt
                if not previous_time or datetime.now(timezone.utc).replace(tzinfo=None) < previous_time + timedelta(hours=48):
                    return reply({'state': 'blocked', 'reason': 'not_due'}, 409)
            cursor.execute("INSERT INTO dbo.ClientSmsEvents(ClientId,Stage,EventKey,State) OUTPUT INSERTED.Id VALUES(?,?,?,'sending')", client.Id, stage, event_key)
            event_id = cursor.fetchone()[0]
            conn.commit()
            state, provider_id = send_sms(phone, text)
            cursor.execute('UPDATE dbo.ClientSmsEvents SET State=?, ProviderId=? WHERE Id=?', state, provider_id, event_id)
            conn.commit()
            return reply({'state': state, 'eventId': event_id})
        except ValueError:
            return reply({'error': 'Invalid phone or SMS configuration'}, 400)
        except Exception:
            # Response/log deliberately omit external responses and contact details.
            logging.error('SMS stage failed; no automatic provider retry performed.')
            return reply({'error': 'SMS unavailable; check configuration and event state before retrying.'}, 503)
        finally:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            for resource in (cursor, conn):
                if resource:
                    try:
                        resource.close()
                    except Exception:
                        pass

    @app.timer_trigger(schedule='0 * * * * *', arg_name='timer',
                       run_on_startup=False, use_monitor=True)
    def submission_sms_jobs(timer: func.TimerRequest):
        process_submission_jobs(connect, document_sms)
        process_document_jobs(connect, document_sms)
