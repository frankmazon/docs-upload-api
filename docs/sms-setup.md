## Correction (2026-10-08)

GHL explicitly documents that an absent global `dnd` field means false across its APIs:
https://marketplace.gohighlevel.com/docs/2021-07-28/webhook/ContactDndUpdate/index.html

The adapter now follows that default while retaining blocks for explicit global DND,
SMS active/permanent DND, opt-out tags, malformed DND values, contact identity and
phone mismatches. Missing GHL phone now returns `missing_ghl_phone` instead of a generic error.
Earlier notes below about requiring an explicitly present false DND field are superseded.
Australian numbers entered with both +61 and the local leading zero are now normalized before comparing portal and GHL values. Sending remains disabled; dry-run verification must pass before activation.

# CompleteSMS document messages

Status (2026-10-07): SMS endpoint deployed to docsuploadpythonapi-flex, server credentials configured, SMS_SENDING_ENABLED=false. All 36 regression tests pass. Direct provider tests previously confirmed Australian mobile delivery; no automated workflow SMS has been sent. GHL action authorization and workflow wiring remain to be completed. Production contact responses checked during deployment omitted DND fields; the endpoint deliberately blocks sending for those records pending explicit opt-out-state verification.

## Configuration

Set server-side settings only:

- `COMPLETESMS_ACCOUNT_ID`: CI006445
- `COMPLETESMS_USERNAME`: info@sbrfunding.com.au
- `COMPLETESMS_API_PASSWORD`: the user's API password, stored in Azure app settings or a Key Vault reference
- `SMS_WEBHOOK_SECRET`: a random server-to-server secret (at least 32 random bytes)
- `SMS_SENDING_ENABLED`: false until a controlled test is ready

Never include either secret in frontend Vite variables, Git, URLs, or screenshots. Rotate the shared API password after setup. Update its Azure value at the same time.

Deploy `sms_integration.py` alongside `function_app.py`, `host.json`, and `requirements.txt`. Existing deployment scripts that copy only function_app.py must include the new module.

## Submission confirmation

Immediately after the existing confirmation email, use the webhook below with
`stage: "submission"`. This acknowledges the scenario rather than claiming a document
was uploaded. It requires no documentId and is deduplicated once per client.
Keep `dryRun: true` until the endpoint is deployed and authenticated preview verified.

## GHL Custom Webhook

POST `https://docsuploadpythonapi-flex.azurewebsites.net/api/sms/document-stage`

Headers:

- Authorization: Bearer <SMS_WEBHOOK_SECRET>
- Content-Type: application/json

Reminder body (select the real client ID custom field with GHL's field picker):

```json
{
  "clientId": "{{contact.client_id}}",
  "stage": "reminder1",
  "dryRun": true
}
```

The client ID must be the portal's `CL-...` ID. The server retrieves the recipient from SQL, then verifies the matching GHL contact, phone and DND flags. Missing/failed GHL checks block sending. A missing DND field also blocks sending; inspect the actual contact response before activation.

Keep existing email actions. Create a separate workflow enrolled once per submitted scenario, with re-entry disabled:

1. Wait 48 hours after submission.
2. Custom Webhook reminder1.
3. Wait 48 hours; Custom Webhook reminder2.
4. Wait 48 hours; Custom Webhook reminder3.
5. Wait 48 hours; Custom Webhook reminder4.
6. Wait 48 hours; Custom Webhook reminder5; end.

Add an If/Else before each action for current outstanding documents if available. The backend independently rechecks SQL. Uploaded files awaiting review and waived requirements do not count as missing. The current shared checklist also excludes rejected uploads from 'missing'; requests for replacement documents require a separate rejected-document workflow.

Reminders are deduplicated once per client/stage. Reminder1 cannot send within 48 hours of submission; later reminders require a queued preceding reminder at least 48 hours earlier. Partial uploads must not re-enroll/reset the sequence. A skipped reminder is not retried automatically. Review execution logs for `blocked`/`unknown` results. The final message does not put the file on hold automatically.

## Upload acknowledgement

The endpoint also supports:

```json
{
  "clientId": "CL-EXAMPLE",
  "stage": "received",
  "documentId": 123,
  "dryRun": true
}
```

`documentId` must be a saved Documents.Id for that client. The upload handler already captures the saved document ID internally; an upload event bridge must pass this to the webhook after a successful commit. **This bridge is not yet wired:** existing GHL contact updates alone do not supply a reliable per-upload document ID. Do not enable a generic contact-changed trigger for receipts: unrelated edits would cause messages. Each saved document ID can produce at most one acknowledgement. Upload batching would require a persistent batch identifier if one message per multi-file batch is desired.

## Testing and activation

Both server setting `SMS_SENDING_ENABLED=true` and JSON `dryRun:false` are required to send. Preview requests do not call CompleteSMS or reserve message events. They check recipient/opt-out/document eligibility, but do not prove provider credentials work or reserve/validate the sequence timing.

Before enabling a workflow, use a nominated test client/phone and inspect one explicit test send. No such test has been performed. Check provider Reports for delivery; API acceptance means queued, not delivered.

Provider API calls are never retried automatically. SQL reserves an event before sending. On timeout or ambiguous response it remains unknown/sending and duplicate requests cannot resend it. Reconcile these records against CompleteSMS Reports before manually correcting event state. HTTP 200 with `state:unknown` is deliberately not a success-to-delivery claim.

`dbo.ClientSmsEvents` is lazily created on the first enabled send with a serialized schema lock. It records stage, client, event identity, UTC time, provider ID and state. It does not store credentials or message bodies.

Replies and delivery receipts currently use provider email forwarding. They are **not** synchronized into GHL Conversations. Until an authenticated reply/opt-out bridge is implemented, staff must monitor provider/email replies and immediately set SMS DND or `sms-opt-out` on the GHL contact for opt-outs. Provider sender identity and reply routing still need verification before automation is activated. The configured provider account daily limit remains applicable; long templates can consume multiple SMS segments.

## Sources

- https://completesms.com/resources/developer-resources/
- https://completesms.com/wp-content/uploads/2025/09/SMSAPI-v1.1-Documentation-060325.pdf
- https://help.gohighlevel.com/support/solutions/articles/155000003305/

The PDF specifies API version 1.1 with numbered message objects; this adapter follows it. The website's Python example uses version 1.1.1 and an SMS array, so do not mix the two formats. Live compatibility still needs a controlled send.

## Webhook token handoff

The generated token is stored outside Git at `/Users/menard/.codex/secrets/sbr-sms-webhook-token.txt` with owner-only permissions. Paste its contents into the GHL Bearer Token field; do not include the word Bearer in that token field. Keep dryRun true. The token must match SMS_WEBHOOK_SECRET in Azure. Do not use the CompleteSMS password as the GHL token.

## Live verification (2026-10-07)

- Azure remote build/deploy succeeded and host reported Running.
- Unauthenticated POST returned 401 with Unauthorized.
- Authenticated production preview returned 200 with state skipped and reason contact_opted_out_or_unverified; GHL contact lookup succeeded but DND fields were absent. No sending permission was inferred from absent fields.
- Some verification calls timed out before a response; existing intermittent Function App availability issues remain unresolved.
- Global sending switch stays false. GHL can save the authenticated dry-run action, but this is not yet an activated SMS workflow.
