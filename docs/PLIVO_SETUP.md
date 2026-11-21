# Plivo outbound setup

The Plivo account/phone requirements are complete and a number has already been purchased. This guide covers wiring that account into this application. Gemini Live continues to generate the voice; no Google Cloud TTS setup is required.

## 1. Backend environment

Edit **`be/.env`**. Never put these values in `fe/.env` or a browser bundle.

```dotenv
OUTBOUND_CALLS_ENABLED=false
PLIVO_AUTH_ID=your_account_or_subaccount_auth_id
PLIVO_AUTH_TOKEN=your_matching_auth_token
PLIVO_FROM_NUMBER=+91YOUR_PURCHASED_NUMBER
PLIVO_ORGANIZATION_ID=your_application_organization_uuid
PLIVO_ALLOWED_COUNTRIES=IN
PUBLIC_BASE_URL=https://your-public-backend.example.com
MEDIA_GRANT_ENCRYPTION_KEY=your_fernet_key
```

| Value | Where it comes from |
| --- | --- |
| `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` | Plivo console account credentials; use the same account/subaccount that owns the calling number |
| `PLIVO_FROM_NUMBER` | Your purchased voice-enabled number, including `+` and country code |
| `PLIVO_ORGANIZATION_ID` | The application organization UUID, available from the organization API/selected organization; not a Plivo identifier |
| `PUBLIC_BASE_URL` | Public **backend HTTPS origin**, with no path/query; use an HTTPS tunnel to port 8000 for local testing |
| `MEDIA_GRANT_ENCRYPTION_KEY` | A Fernet key; one has been generated in the local `.env` during migration. Use a separate stable deployment secret in each environment |

To generate a new encryption key for another environment:

```sh
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Keep the key stable while calls/grants are active. Existing `GEMINI_API_KEY`, `GEMINI_LIVE_MODEL`, `VOICE_NAME`, Firebase, and database settings remain necessary. Old provider environment keys have been removed. The local `.env` has `PLIVO_FROM_NUMBER=+918065593526`, as supplied by you. The Plivo credentials are present locally, and `PLIVO_ORGANIZATION_ID=edd267c3-009e-4447-a801-c50a216cc6a1` was selected from the sole active application organization. Confirm that this is the intended owner before the first test call. The example file keeps placeholders for other deployments.

Optional limits (defaults shown):

```dotenv
CALL_MAX_DURATION_SECONDS=600
CALL_ANSWER_TIMEOUT_SECONDS=45
MEDIA_START_TIMEOUT_SECONDS=10
MEDIA_IDLE_TIMEOUT_SECONDS=30
MEDIA_GRANT_TTL_SECONDS=300
TELEPHONY_MAX_CONCURRENT_CALLS=1
TELEPHONY_DIALS_PER_SECOND=1
```

There is also a 30-attempts/hour/organization cap. Uncertain calls reserve a concurrency slot until resolved; this prevents accidental duplicate calls. Set limits at or below your Plivo account allowances. Canonical destination countries must be in `PLIVO_ALLOWED_COUNTRIES` (comma-separated ISO codes).

## 2. What to configure in the Plivo dashboard

**No incoming-call application, Answer URL, or webhook URL needs to be configured in the dashboard for this outbound integration.**

In the console:

1. Copy the account Auth ID and matching Auth Token into backend secrets.
2. Confirm the purchased number is voice-enabled and belongs to those credentials; copy it to `PLIVO_FROM_NUMBER`.
3. Check balance, permitted destinations, account concurrency/CPS and any trial test-recipient restrictions.

The backend supplies these URLs automatically for each outbound API call:

| Purpose | URL passed by the application |
| --- | --- |
| Answer — required | `https://YOUR_BACKEND/plivo/answer/{attempt_id}` |
| Ring | `https://YOUR_BACKEND/plivo/ring/{attempt_id}` |
| Hangup | `https://YOUR_BACKEND/plivo/hangup/{attempt_id}` |
| Stream lifecycle — in answer XML | `https://YOUR_BACKEND/plivo/stream-status/{attempt_id}` |
| Two-way audio — in answer XML | `wss://YOUR_BACKEND/plivo/media?token=ONE_USE_GRANT` |

Do not paste the `{attempt_id}` templates into the console: the backend creates a real ID for each call. Do not configure the number's incoming flow to use this application. Unsigned HTTP callbacks, incoming-direction callbacks, unknown attempts and reused media grants are rejected. The media WebSocket requires a short-lived, one-use grant issued by the signed answer callback, and its start frame must match the authorized Plivo call and account. Plivo's V3 upgrade signature is checked and logged, but a mismatch does not override a valid one-use media grant.

Plivo's outbound API requires an `answer_url` and supports supplying callback URLs directly. See the [Calls API](https://www.plivo.com/docs/voice/api/calls) and [Stream XML](https://www.plivo.com/docs/voice/xml/audio-streaming).

## 3. Public ingress and deployment

- Expose port 8000 through HTTPS with a valid certificate and WebSocket upgrade support.
- Forward `/plivo/*` without login redirects, path rewriting, body modification or stripping `X-Plivo-Signature-V3` / `X-Plivo-Signature-V3-Nonce`.
- Keep the public hostname identical to `PUBLIC_BASE_URL`. Signature validation reconstructs this configured HTTPS URL, including path/query, rather than trusting proxy host headers. WebSocket upgrade signatures are validated against the HTTPS upgrade URL using the official SDK.
- Redact the query string on `/plivo/media` in reverse-proxy, load-balancer and error-reporting logs. The application clears it before accepting/rejecting the socket; upstream logs need their own redaction. Do not log answer XML bodies or authorization headers.
- Use a WebSocket frame limit of 64 KiB and a bounded receive queue. Set proxy timeouts to permit the configured call duration. Let active calls drain before restarting a process.
- Updating a tunnel URL requires updating `PUBLIC_BASE_URL` and restarting the backend before placing new calls. No console webhook edit is needed.
- Authentication must stay enabled during local testing; do not bypass signatures to fix a tunnel mismatch.

## 4. Migrate and start

From `be/`:

```sh
uv sync --group dev
uv run alembic upgrade head
uv run python -m scripts.check_database
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 65536 --ws-max-queue 16
```

`0005` preserves historical records and adds call/session correlation, grant storage, and reconciliation metadata. Fresh installs use the existing bootstrap roles plus `alembic upgrade head`; the bootstrap script does not contain application table DDL.

Before replacing a running old deployment, stop new dials and drain its calls. Deploy the Plivo-only build, fill the required settings, set `OUTBOUND_CALLS_ENABLED=true`, then restart. The enabled startup checks fail on incomplete configuration. Telephony reconciliation runs automatically whenever Plivo credentials, organization and database are configured, including when new dialing is disabled.

The configured database was upgraded from `0004` to `0005` on 2026-10-09 and passed `scripts.check_database`. Other environments still require their own migration. Use migration credentials only for Alembic, and the runtime role for the application.

## 5. Place one authorized test call

In the UI, use **Add lead** to enter one contact or **Import leads** for a sheet. Both paths save the phone as a voice contact with calling permission unknown. Open the lead, choose **Record permission**, and record how the contact agreed. Then choose **Start AI call**. The drawer refreshes call status automatically; Plivo's signed callbacks update the call and conversation, and an answered completed call changes a new lead to `contacted`. The agent connects to Gemini Live using the backend `GEMINI_API_KEY`; no browser-side Gemini key is needed. If the call status remains initiated, check callback delivery before placing another call. The UI does not automatically redial uncertain attempts.

For API use, use a member/org-admin Firebase ID token, an existing eligible voice contact, and the exact contact number. The contact must be unsuppressed; adding or importing a lead alone does not authorize calling.



```http
POST /api/orgs/YOUR_ORG_UUID/calls
Authorization: Bearer YOUR_FIREBASE_ID_TOKEN
Content-Type: application/json

{
  "contact_id": "YOUR_CONTACT_UUID",
  "to_number": "+91TEST_RECIPIENT_NUMBER",
  "idempotency_key": "a-new-unique-request-key-001"
}
```

Response:

```json
{
  "call_id": "internal-session-uuid",
  "attempt_id": "internal-attempt-uuid",
  "provider": "plivo",
  "status": "initiated",
  "provider_request_id": "plivo-request-uuid",
  "provider_call_id": null,
  "end_reason": null,
  "cleanup_pending": false
}
```

`initiated` means accepted, not answered. The call UUID arrives via signed callbacks. Read updates using `GET /api/orgs/YOUR_ORG_UUID/calls/CALL_ID` with the same application authorization. `unknown` means the provider outcome is uncertain: **do not issue a new call with another key**. Repeating an existing key is rejected.

The former `call_sid`/`stream_url` response fields are removed. Audio URLs and grant tokens are backend-only. State is persisted in `CallSession`; the linked authorization row handles admission/routing. A full transcript/recording workflow is not added.

## 6. Verification and troubleshooting

Test a short multi-turn call with the purchased number: exact opening greeting, intelligible Malayalam both ways, interruption stops previous speech, customer hangup releases resources, and final status appears in the API and Plivo logs. Also test rejection/no-answer and an interrupted network connection. Linear PCM 8 kHz is explicit; validate byte order/audio quality on the actual route before broad rollout.

| Symptom | Check |
| --- | --- |
| 503 calling disabled | `OUTBOUND_CALLS_ENABLED` and required settings; restart after environment edits |
| 503 account not assigned | `PLIVO_ORGANIZATION_ID` matches the API organization |
| 403 contact ineligible | Voice channel and contact suppression/preference; do not disable this check |
| 403 webhook/signature | Matching account token, exact public origin/path/query, intact POST fields and signature headers |
| Answer then silence | Answer request/XML in Plivo logs, public WSS upgrades, media start format, Gemini credentials/model and backend error type |
| Distorted sound | Actual stream codec/rate and PCM byte order; do not feed mu-law into the PCM converter |
| 429 local limit | Existing active/unknown calls, hourly limit or dial rate; check status before retrying |
| `unknown` | Review Plivo call logs using request/call/attempt IDs; the application never automatically redials |
| `cleanup_pending=true` | Provider control failure; reconciler retries cancellation/hangup |

If an attempt has no provider identifiers, first establish its outcome in the Plivo console. Only after confirming that **no call was placed**, a deployment operator can release the reservation:

```sh
uv run python -m scripts.resolve_call --attempt-id ATTEMPT_UUID --confirm-not-placed --reason "Verified no call was placed in Plivo logs; support reference ..."
```

This audited command rejects active/identified calls and keeps the original idempotency key consumed. Do not use it when a call may exist, bulk-reset unknown attempts, or fabricate IDs. The reconciler never matches calls by number/time heuristics.

For a one-shot reconciliation pass:

```sh
uv run python -m app.telephony.worker
```

For an incident, disable new outbound calls and restart/drain safely; retain callbacks and reconciliation. Repair the Plivo path before enabling calls again. There is no second telephony provider fallback.

[V3 signature validation](https://www.plivo.com/docs/voice/concepts/signature-validation) · [Audio streaming protocol](https://www.plivo.com/docs/voice-agents/audio-streaming/concepts/audio-streaming-reference)
