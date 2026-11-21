# Google Sheets connection

This integration exports **app → Google Sheets** updates using the receiver script
and the backend's background exporter.
Editing the sheet does not change app records, grant calling permission, send a
webinar link, or start calls. `Send Webinar Link` records the AI result only.

## Install in your blank sheet

1. Open your blank Google Sheet → **Extensions → Apps Script**.
2. Replace all of `Code.gs` with the contents of the adjacent `Code.gs` file. Save.
3. Select **setupTracker** in the function dropdown and click **Run**. Authorize
   the script with the account that owns the sheet. If Workspace policy blocks
   authorization or anonymous web apps, the account administrator must allow it.
4. Return to the sheet if necessary: a dialog shows a generated **connection code**.
   Copy it. Reload the sheet to get the **AI Sales → Show connection code** menu
   if you need it again. This is this integration's secret, not a Google login,
   OTP, OAuth authorization code, or Plivo credential.
5. In Apps Script choose **Deploy → New deployment → Web app**:
   - **Execute as:** Me.
   - **Who has access:** Anyone (the backend authenticates using the code).
   - Click **Deploy** and copy the deployed URL ending in **/exec**, not `/dev`.
6. Share the **/exec URL**, generated **connection code**, and **spreadsheet URL**
   for backend configuration. The sheet itself can remain private.

Google's deployment guide: https://developers.google.com/apps-script/guides/web

If you already ran the earlier script, replace its code and run **setupTracker**
again. Version 3 removes the old Organization ID column, fixes checkbox formatting,
and moves any misplaced leads up to row 2 while preserving their other columns.
Redeploy as a new version if already deployed. The connection code stays the same.

## Formatting and chips

The script creates one **Leads** tab with frozen headers and identity columns,
filters, readable column widths, wrapped summaries, typed dates shown in IST,
colored status cells, dropdown validation, and a webinar-link checkbox.

Dropdowns are created for **Lead Status (G), Result (H), Call Status (I),
Webinar Response (M), and Genuineness (O)**. To ensure pill-shaped chips:

1. Select **G2:G1001** → **Data → Data validation** → select its existing rule.
2. Expand **Advanced options** → **Display style → Chip** → **Done**.
3. Repeat for **H2:H1001**, **I2:I1001**, **M2:M1001**, and **O2:O1001**.
4. Optionally choose the individual option colors in that sidebar.

Apps Script's documented validation builder does not expose a chip-display or
individual-option-color setting; the script supplies conditional cell colors.
Actual chip display and chip colors are set in the Sheets UI.
https://developers.google.com/apps-script/reference/spreadsheet/data-validation-builder

**A:S** are managed by the app. **T:V** (Owner, Follow-up At, Team Notes) are
manual team fields that API updates preserve. Keep the headers and IDs intact.
Sort the entire table, not individual columns. Do not reuse or duplicate lead IDs.
Re-running setup preserves rows and the code, but refreshes formatting and dropdown
rules, so reselect Chip afterward. After 1,000 leads, rows and formatting expand;
check Chip display on newly added ranges too.

## Backend configuration

Keep these values in the backend's ignored `.env`, never in frontend settings:

```dotenv
GOOGLE_SHEETS_ENABLED=true
GOOGLE_SHEETS_WEB_APP_URL=https://script.google.com/macros/s/YOUR_DEPLOYMENT/exec
GOOGLE_SHEETS_ACCESS_CODE=YOUR_GENERATED_CODE
GOOGLE_SHEETS_ORGANIZATION_ID=YOUR_APPLICATION_ORGANIZATION_UUID
GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS=15
```

The organization setting scopes the database read internally. No organization ID
is sent to Apps Script or displayed in the sheet. Restart FastAPI to enable its
background worker. It backfills existing leads, then exports changed snapshots
roughly every 15 seconds plus Google's response time. Failures retry with bounded
backoff; calls and lead imports never wait for Sheets. It does not delete sheet
rows when application records are deleted. Run one backend worker for this local
POC; multiple exporters would duplicate Google requests.

The latest call supplies the current call columns. Starting a new call clears the
previous result until the new summary is available. In-process live call states
are included; the durable completed summary is exported after persistence.

Read-only connection check and optional one-off backfill from `be/`:

```sh
.venv/bin/python -m scripts.sync_google_sheets --check
.venv/bin/python -m scripts.sync_google_sheets
```

The second command writes existing application leads to the sheet; neither command
places calls or creates application leads. Check backend logs for connection,
export counts and retry categories. The exporter checks JSON success as well as
HTTP status and follows Google's content-service redirects.

## Backend request contract

Send JSON using POST to the deployed `/exec` URL. Follow Google's response
redirects. Never put the connection code in URL query parameters or frontend code.
Apps Script JSON errors use HTTP 200 too: always check `ok` in the JSON response.

Authenticated connection check:

```json
{"access_code":"YOUR_GENERATED_CODE","action":"ping"}
```

Create or update a lead:

```json
{
  "access_code": "YOUR_GENERATED_CODE",
  "action": "upsert_lead",
  "lead": {
    "lead_id": "app-lead-uuid",
    "name": "Example Lead",
    "phone": "+919876543210",
    "email": "lead@example.com",
    "source": "Website",
    "language": "ml",
    "lead_status": "contacted",
    "result": "WARM",
    "call_status": "completed",
    "provider_status": "completed",
    "summary": "Interested in the course; requested a callback.",
    "preferred_callback_time": "Tomorrow after 6 pm",
    "webinar_response": "YES",
    "send_webinar_link": true,
    "genuineness": "CONFIRMED",
    "duration_seconds": 92,
    "call_id": "app-call-uuid",
    "last_call_at": "2026-10-31T11:30:00Z",
    "updated_at": "2026-10-31T11:32:00Z"
  }
}
```

- One row per **lead_id**; it holds the latest exported lead
  and call result. Historical calls remain in the app.
- `lead_id` and `updated_at` are required. Other fields are optional; omitted
  fields retain their existing values. Explicit `null` clears an optional field,
  except `send_webinar_link`, which must be a JSON boolean.
- Use complete, timezone-qualified ISO timestamps. The backend must use a stable,
  monotonically increasing snapshot version time covering lead changes, call
  changes, and late AI summaries. Reuse that time when retrying the same snapshot.
  For a new latest call, explicitly clear prior call summary/result fields until
  the new result is available. Do not combine unrelated, out-of-order partial
  updates; send authoritative snapshots in order.
- Older or equal versions return `{"ok":true,"result":"skipped_stale",...}`.
  Successful writes return `created` or `updated` and a row number. Row numbers
  can change after sorting; identity is always `lead_id`.
- Unknown fields and invalid dropdown values are rejected before writing.
  Requests are serialized with a script lock. A `busy` response is retryable.
  Retry network failures with backoff and the same snapshot timestamp.
- Public GET returns only service health; there is no public lead-read endpoint.
- Connection code rotation: change `ACCESS_CODE` in Apps Script **Project
  Settings → Script properties** and update the backend's secret to match.

Changes to script code require **Deploy → Manage deployments → Edit → New
version → Deploy**. Script-property changes alone do not need redeployment.

## Verification

Local checks cover JavaScript syntax, authentication, validation, row identity,
formula escaping, stale retries and preservation of manual fields. Google
authorization, native formatting, chips and the deployed endpoint must be checked
after deployment. Backend tests use an isolated SQLite database and mocked Google
requests, including tenant isolation, latest-call selection, late summaries,
redirects, failure retries and preservation of sensitive provider errors.
