# Firebase authentication and organization administration

The React app now requires Firebase login. FastAPI verifies ID tokens and current
PostgreSQL roles on every request. Organization business tables use PostgreSQL RLS
and organization-aware foreign keys. The database is authoritative; Firebase
custom claims are a compact, asynchronously synchronized access snapshot.

## Configure Firebase

1. Create separate Firebase projects for development, staging, and production.
2. Register a Web app and enable Authentication → Email/Password.
3. Add the actual frontend hosts to Firebase authorized domains. Configure email
   verification and password reset templates. Verification must be completed before
   application access is granted.
4. Copy `fe/.env.example` to the ignored `fe/.env.local`, and set the four
   `VITE_FIREBASE_*` values. These are public Firebase web configuration values.
5. Set `FIREBASE_PROJECT_ID` in the backend. Use Application Default Credentials
   with an appropriate service account, preferably workload identity in deployment.
   For local development, `GOOGLE_APPLICATION_CREDENTIALS` may point to a service
   account file **outside the repository**. Never put Admin credentials in Vite.
6. Set a Firebase password policy, enable email enumeration protection, and apply
   appropriate Firebase API quotas. Account creation alone never grants org access.

For a local Firebase Auth emulator, set `FIREBASE_AUTH_EMULATOR_HOST` on the backend
and `VITE_FIREBASE_AUTH_EMULATOR_URL` on the frontend; use the same project ID.
Emulator tokens are accepted only when `APP_ENV=development`. Do not deploy emulator
settings. The frontend excludes emulator connection code outside Vite development.

## Database migration and restricted runtime role

Take a backup first. **Do not run the integration tests against a shared database.**

The old pilot could use `postgres` for runtime access. Authentication now rejects
superuser, RLS-bypass, role-management, or application table-owner credentials.
Use separate `DATABASE_URL` and `MIGRATION_DATABASE_URL` values. The runtime URL must
use `ai_sales_runtime`, with its own password; the migration URL should include
its separate migration password or use deployment-managed credentials.

If the runtime role does not exist, create it as database administrator before
migration:

```sql
CREATE ROLE ai_sales_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
```

Set its password securely with your database administration tooling, not a committed
SQL file. On an existing project, review role membership and table ownership.
Migrations grant required access to this role when it exists. For production,
remove inherited DDL privileges as explained in `SUPABASE_SETUP.md`.

From `be/`:

```sh
uv sync
uv run alembic upgrade head
uv run alembic check
uv run python -m scripts.check_database
```

Migration `0004` creates identity/RBAC tables, organization keys, cross-org foreign
keys, RLS policies, org-specific import fingerprints, and invitation/audit queues.
Existing business data is retained in a **suspended legacy workspace** with ID
`00000000-0000-4000-8000-000000000001`. Review ownership before assigning an admin
and activating it. No user automatically receives this data.

Existing WhatsApp pilot connections retain their old session owner and cannot be
claimed through a browser cookie. Reconnect under the intended organization after
operator review; an operator must release an old phone reservation if needed.
Do not blindly remap or expose old provider credentials.

This migration deliberately refuses automatic downgrade: removing tenant columns
could expose shared data through the old anonymous APIs. Roll back using a reviewed
backup or a compatible secured application version.

## Bootstrap the platform administrator

The CLI uses the existing backend `.env` (`FIREBASE_PROJECT_ID`,
`GOOGLE_APPLICATION_CREDENTIALS`, and database configuration). Run migrations first.
From `be/`, create the main super-admin account with:

```sh
uv run python -m scripts.bootstrap_admin --email admin@your-company.com --create --name "Main Admin"
```

The script checks the database before Firebase account creation, finds the selected
email, and creates it only if missing and `--create` was supplied. For a new account,
it prompts twice for a hidden password of 12–128 characters. Run it in an interactive
terminal; passwords are never accepted through CLI arguments or printed.

To promote an existing Firebase account, use either:

```sh
uv run python -m scripts.bootstrap_admin --email admin@your-company.com
uv run python -m scripts.bootstrap_admin --uid FIREBASE_UID
```

The command grants the application's `platform_admin` role, writes an audit event,
and immediately synchronizes Firebase custom claims. Repeating it is safe: it
preserves existing passwords and does not duplicate the platform-admin grant or
increment the permission version without a role change. Disabled Firebase accounts
and suspended application users are rejected; the script does not reactivate them.
It never grants the first arbitrary signup administrative rights.

New accounts remain **email-unverified**. Sign in to the frontend, choose **Resend
email** on the verification screen, follow the email link, and continue. If admin
MFA is required, enroll through Security and sign in again with your authenticator.
The CLI does not mark email verified, bypass MFA, or send emails by itself.

Exit codes: `0` means database provisioning and claims synchronization succeeded;
`1` means setup failed; `2` after provisioning means the database grant was saved but
claims need retry (argument errors also use `2`); `130` means interrupted. If claims
fail, rerun the same command or run the auth worker. If Firebase creation succeeds
but a later database step fails, the Firebase user is retained without application
access; rerun with the same email after fixing the error. No automatic deletion or
rollback of an external account is attempted.

To explicitly assign the selected operator an organization admin membership
(including the reviewed legacy workspace), use:

```sh
uv run python -m scripts.bootstrap_admin --uid FIREBASE_UID --organization ORGANIZATION_UUID
```

This optional flag grants that organization membership, including restoring a
previously removed/suspended membership. It does not activate a suspended org.
Resume the org in the platform UI after completing ownership review. Run this
privileged CLI only as a trusted operator.

## Invitation email and claims worker

Set `APP_FRONTEND_URL` to the exact frontend URL including `/ui/`.
Generate a dedicated Fernet key for `AUTH_OUTBOX_KEY` and store it securely. This
protects invitation tokens while email delivery is queued. Configure `SMTP_HOST`,
`SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM`; STARTTLS is enabled
by default. SMTP credentials and the outbox key are backend-only.

Run a supervised worker alongside FastAPI:

```sh
uv run python -m app.auth.worker
# Or one bounded cycle for operational checks:
uv run python -m app.auth.worker --once
```

The worker delivers invitation emails with retries, discards expired/revoked queued
messages, and clears encrypted tokens after delivery. Delivery is at least once;
acceptance is single use and safe to retry. Failed deliveries appear in the Team
invitation history; resend creates a new token and invalidates old links.

The worker also synchronizes `platform_role`, global `permissions`, and
`permissions_version` to Firebase custom claims. Organization roles and detailed
permissions remain in PostgreSQL and `/api/me`. Claims writes are serialized with
role updates and retried after failure. Firebase claims have a 1,000-byte limit;
we do not copy unlimited organization memberships into tokens. The worker owns the
whole custom-claims object for this application.

A delayed or failed Firebase claims update never restores denied access: FastAPI
uses current database membership/status. Run the worker continuously and monitor
its logs and pending delivery/claim status.

## Admin MFA and recovery

Enable TOTP MFA in Firebase Authentication with Identity Platform (an upgrade is
required). In the frontend, use **Security → Set up authenticator**, add the setup
key to an authenticator, and verify its code. Then sign out and sign in with TOTP.

Set `AUTH_REQUIRE_ADMIN_MFA=true` to enforce verified second-factor login on platform
and org admin operations. `APP_ENV=production` refuses startup without this flag.
A signed-in user can still open Security and enroll before entering admin screens.
Sensitive admin changes require authentication within the last ten minutes; sign
out/in when prompted. Normal members and viewers do not require MFA.

If an administrator loses their factor, an operator must verify the person's
identity using an established independent process, reset their MFA enrollment via
Firebase administration, revoke their refresh tokens, and record the recovery.
The user must re-enroll and sign in with a second factor. No UI bypass or shared
recovery password is provided. Maintain more than one trusted platform operator.

## Running the application

```sh
# fe/
npm ci
npm run dev
# be/ (separate terminal)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# be/ (another terminal)
uv run python -m app.auth.worker
```

For the same-origin production app, build `fe/` with `npm run build` and open `/ui/`
on FastAPI. Keep HTTPS enabled at the reverse proxy. Restrict credential-bearing
logs; specifically redact query strings for `/exotel/media` at every ingress since
its one-use stream credential travels in the provider's callback URL.

The frontend includes login, reset, verification, MFA, invitation acceptance,
organization selection, platform management, Team, Invitations, Settings, Audit,
and the existing Leads/WhatsApp workspace. Members can import, viewers cannot,
and only org admins can configure WhatsApp.

Old `/api/leads`, `/api/imports`, `/api/whatsapp`, and `/test-call` routes are removed.
Business requests use `/api/orgs/{org_id}/...` and a Firebase bearer ID token.
Provider webhooks use provider signatures, not Firebase credentials.

Exotel credentials are explicitly assigned via `EXOTEL_ORGANIZATION_ID` to one org
per deployment. Calls require an eligible, unsuppressed contact, matching number,
and an idempotency key. Media ingress requires a one-use expiring call token and
periodically rechecks org/member status. Multiple independent org-owned Exotel
accounts and inbound routing are not configured by this release. Actual Exotel
callback delivery must be verified in staging before production calls.

## Verification

Unit/UI tests and production build:

```sh
# be/
uv run pytest -q
# fe/
npm test
npm run build
```

Real PostgreSQL integration tests require a **dedicated disposable database**, a
restricted runtime role and separate administrator role. Create placeholder
Supabase roles `anon`, `authenticated`, `service_role` plus `ai_sales_runtime` and
`ai_sales_migrator` there; run migrations, then set:

```sh
TEST_DATABASE_URL=postgresql://ai_sales_runtime@127.0.0.1:55439/relay_auth_test
TEST_MIGRATION_DATABASE_URL=postgresql://postgres@127.0.0.1:55439/relay_auth_test
TEST_AUTH_DATABASE_URL=postgresql://ai_sales_runtime@127.0.0.1:55439/relay_auth_test
TEST_AUTH_ADMIN_URL=postgresql://postgres@127.0.0.1:55439/relay_auth_test
```

Export those values before running pytest. Integration tests create test records
and exercise rejected migration downgrade; use no shared/live database credentials.
Authentication claims and mail/providers are mocked in these tests. Real Firebase
email delivery, token revocation, MFA, SMTP, and provider callbacks still require
configured staging services for an end-to-end deployment check.

### Invitations without email setup

When SMTP_HOST or SMTP_FROM is unset, creating an invitation returns a one-time-displayed shareable link to the authorized administrator. Use Create invitation and Copy link in the Team screen. The recipient signs in with the invited email address and accepts it; the link expires after 72 hours. Only its hash is stored, so generate a new invitation if the link is lost (the old link is revoked). No SMTP worker or outbox encryption key is required for this mode. Set APP_FRONTEND_URL to the frontend address recipients can reach. Configuring SMTP enables the existing encrypted email delivery flow.
