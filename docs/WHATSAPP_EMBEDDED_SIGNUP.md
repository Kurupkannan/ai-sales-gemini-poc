# Private WhatsApp Embedded Signup prototype

This release connects a controlled test business through Meta Embedded Signup. It includes the React WhatsApp screen, server-side code exchange, encrypted token storage, token/app/permission verification, number selection, app subscription, optional new-number registration, signed webhook receipt tracking, retry, and local disconnect.

It does **not** send messages, build a messaging inbox, or onboard public customers. Existing application login remains deferred. The pilot is bound to a short-lived browser session and one fixed `private-pilot` workspace. Keep the whole operator app/API behind local or network access controls. Exact-origin checks and CSRF protection are not substitutes for customer authentication.

## Configure later

All configuration goes in `be/.env` or deployment secrets. No Meta secrets or Vite environment settings are needed in the frontend.

| Setting | Value/source |
| --- | --- |
| `WHATSAPP_PROTOTYPE_ENABLED` | `true` only in the restricted pilot environment; default `false` |
| `META_APP_ID` | Platform app ID from Meta App Settings |
| `META_APP_SECRET` | Platform app secret from Meta App Settings; backend only |
| `META_EMBEDDED_SIGNUP_CONFIG_ID` | Facebook Login for Business / current Embedded Signup configuration ID for WhatsApp |
| `META_GRAPH_API_VERSION` | Supported Graph version configured for your app, formatted `vNN.0`; intentionally no guessed default |
| `META_WEBHOOK_VERIFY_TOKEN` | A random value you choose; enter the identical value in Meta webhook configuration |
| `WHATSAPP_ENCRYPTION_KEY` | One persistent Fernet key generated below; backend only, backed up separately from the database |
| `WHATSAPP_ALLOWED_ORIGINS` | Exact frontend origins separated by commas, e.g. `https://pilot.example.com`; no paths or trailing slash |
| `WHATSAPP_COOKIE_SECURE` | `true` for HTTPS. `false` is accepted only when all allowed origins are local `http://localhost` or `http://127.0.0.1` origins |
| `DATABASE_URL`, `MIGRATION_DATABASE_URL`, database TLS/password settings | Existing PostgreSQL configuration; see `SUPABASE_SETUP.md` |

Generate your encryption key once, from `be/`:

```sh
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Do not regenerate it on startup: doing so makes stored tokens unreadable and expires browser sessions. Do not paste tokens or secrets into frontend code. Customer access tokens, WABA IDs, and number IDs are obtained/verified during signup and are not global `.env` credentials.

## Install and migrate

From `be/`:

```sh
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Migration `0003` adds `app.whatsapp_connections` and `app.whatsapp_webhook_receipts`. It does not alter existing lead records. Migrations never run automatically at application startup. Use the configured migration role; do not use runtime credentials for schema changes.

From `fe/`:

```sh
npm ci
npm run build
```

FastAPI serves the built app at `/ui/`; open `/ui/#whatsapp`. For frontend development, `npm run dev` serves port 5173 and proxies `/api` to port 8000. Use the backend's `/webhooks/whatsapp` for Meta callbacks (Vite only proxies `/api`). Prefer a single HTTPS frontend/backend origin for the actual Meta test and configure it both here and in Meta. A local HTTP UI is useful for development but does not guarantee Meta will permit signup on that origin.

## Meta setup before a real test

1. Configure your platform-owned app for WhatsApp and Facebook Login for Business/Embedded Signup, including the current supported signup configuration. The frontend uses `config_id`, `response_type: code`, and the JavaScript SDK; product/permission choices belong in the Meta configuration. It does not hard-code a legacy v2/v3 flow.
2. Configure allowed JavaScript SDK domains and applicable OAuth redirect settings for your pilot HTTPS domain. Enable the login settings required by the current Meta configuration.
3. Use eligible app administrators/developers/test businesses during development. External customer launch requires applicable business verification, Tech Provider onboarding, App Review/advanced access, plus our own application authentication and tenant isolation.
4. Configure webhook callback `https://YOUR_BACKEND/webhooks/whatsapp` and the chosen verify token. Subscribe to the WhatsApp `messages` field in Meta. The backend additionally subscribes your app to the chosen WABA.
5. Keep public ingress restricted to the provider routes; the operator UI and `/api` stay private. Set reverse-proxy access logs to omit webhook query strings (the verify token is in the GET challenge). The application redacts those queries from Uvicorn access logs and suppresses credential-bearing Graph request URLs.

## Test flow

1. Open WhatsApp in the sidebar. Missing environment settings appear as a checklist; the flow is disabled until configured.
2. **Prepare connection** creates a 15-minute, single-use attempt bound to an HttpOnly, SameSite browser session. **Continue with Meta** opens the popup directly from the click so popup permissions are preserved.
3. Complete Meta's screens. The code is exchanged immediately on the backend and consumed only once, even on uncertain failures. The encrypted token is saved before subsequent verification so transient checks can be retried.
4. Verify the WABA and load its phone numbers. SDK session events prefill IDs when available; token granular scopes supply candidate WABAs where available. If Meta omits the ID, paste the WABA ID from WhatsApp Manager. This is only a hint: Graph API authorization and number membership are checked before subscription.
5. Select **Connect number**. The backend verifies the phone belongs to the WABA, subscribes the app, and confirms the subscription through Meta. “Account connected” does not claim billing or messaging readiness.
6. For a **new**, ownership-verified Cloud API number that still needs registration, expand the optional registration form. Confirm the number type and enter a six-digit two-step-verification PIN. The PIN is sent to Meta but never stored. Keep it securely yourself. This action is not for existing registered numbers, migrations, or Business App coexistence.
7. Send a test webhook from Meta. Refresh to see the signed receipt timestamp. This prototype stores only a payload digest/count and updates the account's last receipt timestamp; it intentionally does not store incoming message bodies or delivery events for a future inbox.

## Recovery and limits

- **Popup blocked/cancelled:** allow popups, abandon the attempt if necessary, and prepare a new one. Late callbacks from an abandoned popup are ignored.
- **Code exchange failed/interrupted:** start a new signup. Reusing a consumed authorization code is unsafe. An interrupted exchange may remain “Authorization submitted”; disconnect it and restart.
- **Verification/subscription failure:** use Recheck authorization, Load numbers, then Connect number. Setup uses a database lease; after a server crash, wait up to 10 minutes for the lease to expire before retrying.
- **Registration timeout:** Meta might have registered the number. Check WhatsApp Manager before retrying. Billing/quality/number eligibility remain visible in Meta; this prototype does not infer them from subscription success.
- **Disconnect locally:** removes the stored token and clears the number reservation. It does not remove a shared WABA subscription or revoke the Meta business grant; use Meta Business Integrations for grant revocation. Late in-flight setup cannot revive a disconnected local connection.
- **Browser/session expiry:** sessions last 24 hours and renew when the screen initializes. Keep cookies for the test; other browsers cannot access the saved connection. If the cookie is lost or expires, an operator must revoke the old grant in Meta and clear the old pilot connection through a controlled database maintenance operation before reconnecting the same number. There is intentionally no unauthenticated claim/recovery endpoint.
- **Retention:** prototype rows are not automatically purged. Remove test data through a reviewed maintenance operation after the pilot. Only encrypted tokens are stored; backups still need appropriate access controls.
- Public customer onboarding, durable messaging queues, AI replies, messaging consent policy, and a full inbox remain later phases.

## Validation

```sh
# Unit tests: no Meta credentials/network required
uv run pytest -q
# Integration tests: ONLY explicitly configured disposable PostgreSQL
TEST_DATABASE_URL=postgresql://ai_sales_runtime@127.0.0.1:PORT/postgres \
TEST_MIGRATION_DATABASE_URL=postgresql://ai_sales_migrator@127.0.0.1:PORT/postgres \
uv run pytest -q
```

The integration suite includes destructive migration round trips and must never target shared/live databases. Tests use mocked Meta responses to verify encrypted persistence, one-time exchange, expired/replayed attempts, cross-session/CSRF protection, number ownership, duplicate webhook receipts, registration, and disconnect racing with setup. Frontend verification: `npm test` and `npm run build`.

Meta references: [Embedded Signup collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/du6gzjv/embedded-signup), [Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api). The developer pages were rate-limited during implementation; the exact live app/configuration and Meta approval requirements must be checked in the dashboard during credential setup. A real Meta signup cannot be validated without those settings.
