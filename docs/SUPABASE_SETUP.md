# Supabase connection and lead workspace

The backend uses one Supabase PostgreSQL database. The current local setup uses
your existing `postgres` account for both API access and migrations. No Supabase
Auth or application login is required; future authentication is deferred to SSO
or Firebase Auth.

## Current configuration

Copy the PostgreSQL connection string from **Supabase → your project → Connect**.
For the session pooler, keep port **5432**, the exact hostname, and the project
suffix on the username. Store the password separately in the ignored `be/.env`:

```dotenv
DATABASE_URL=postgresql://postgres.PROJECT_REF@COPIED_POOLER_HOST:5432/postgres
MIGRATION_DATABASE_URL=postgresql://postgres.PROJECT_REF@COPIED_POOLER_HOST:5432/postgres
DATABASE_PASSWORD='your-actual-database-password'
DB_SSLMODE=verify-full
DB_SSLROOTCERT=/absolute/path/to/supabase-ca.crt
```

Both URLs point to the same database and can be identical. The raw password needs
no URL encoding when supplied through `DATABASE_PASSWORD`. Embedded passwords in
URLs remain supported and take precedence over the shared password.

Direct connections use username `postgres` and host `db.PROJECT_REF.supabase.co`.
Use a direct connection when your deployment supports its IP version; the session
pooler works on IPv4 networks. Transaction mode on port 6543 is not supported.

The shared Supabase pooler requires its CA certificate for verification. Download
it from **Database settings → SSL Configuration** and set its full local path.
The project's local connection has been verified using the Supabase Root 2021 CA
saved outside the repository under `~/.config/ai-sales/supabase-ca.crt`.
An empty certificate setting uses system trust, which may not trust Supabase's
private CA. Keep `verify-full` enabled.

## Migrate and verify

From `be/`, after installing dependencies with `uv sync --group dev`:

```sh
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m scripts.check_database
.venv/bin/python -m alembic check
```

Alembic creates the private `app` schema when needed. No manual bootstrap script
is needed for the existing `postgres` account. Migrations are explicit commands;
the API does not automatically migrate at startup.

The connection checker reports that `postgres` has administrative database access,
then verifies the schema version and tables. The browser never receives this
credential. Keep `app` out of Data API exposed schemas; migrations revoke access
for anonymous/browser-facing roles.

## Lead workspace

Build the frontend from `fe/`:

```sh
npm ci
npm run build
```

Start FastAPI from `be/`:

```sh
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/ui/**. Restart the backend after the first frontend
build so it discovers `fe/dist`. For frontend development, run `npm run dev` in
`fe/` and open **http://127.0.0.1:5173/ui/**; Vite proxies `/api` to port 8000.

Upload UTF-8 CSV or XLSX (first worksheet), map the phone column and optional
fields, preview validation, then confirm. Limits are 5 MB and 5,000 lead rows.
Country defaults to India for numbers without a country code. Download the CSV
template from the workspace to see supported headings.

Repeated files with the same mapping/country are idempotent. Existing phone
numbers and repeated numbers in a file are skipped, never merged or overwritten.
The preview and final result may differ if another import completes in between;
the final result is authoritative. Extra columns appear in lead details. Up to
100 row issues are retained with each import summary. Original files are not
stored. Imported contacts start suppressed with consent unknown, and uploading
never initiates a call or message.

Existing Exotel/Gemini routes remain separate; saving call transcripts is still a
future phase. `/health` is liveness; `/api/database/status` checks schema readiness.
`PUBLIC_BASE_URL` is only for Exotel callback/audio ingress, not database access.

## Optional restricted roles

The separate `ai_sales_runtime` / `ai_sales_migrator` account setup is optional.
For a new project where you choose that setup, run
[`bootstrap_database.sql`](../scripts/bootstrap_database.sql) as administrator
and set distinct passwords, then configure each URL with its role's credentials.
The script creates roles/schema and intentionally fails if they already exist;
do not run it on the current initialized project without adapting it first.
Existing restricted-role deployments remain supported by migrations and checks.

## Tests

```sh
.venv/bin/python -m pytest -q
```

Database integration tests require `TEST_DATABASE_URL` and
`TEST_MIGRATION_DATABASE_URL` pointing to a dedicated disposable local PostgreSQL
instance. They **downgrade and recreate app tables**; never use live/shared data.
Local fixtures require placeholder `anon`, `authenticated` and `service_role`
roles plus the optional restricted roles from the bootstrap script. Local tests
use TLS disabled for loopback only. Tests cover migrations, role restrictions,
parsing, normalization, duplicate/concurrent import handling and lead APIs.

## Troubleshooting

- Certificate error: check `DB_SSLROOTCERT` and the CA matching your endpoint.
- Authentication error: use the database password and correct pooled username.
- Missing schema/table: run migrations using `MIGRATION_DATABASE_URL`.
- Network error: check the copied host, project availability and IP support.
- UI database error: run the connection checker and restart the backend after
  configuration changes. Credentials never belong in frontend files.

## References checked

- [Supabase connection methods and TLS](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase SSL connection example](https://supabase.com/docs/guides/database/psql)
- [PostgreSQL roles](https://supabase.com/docs/guides/database/postgres/roles)
- [FastAPI file uploads](https://fastapi.tiangolo.com/tutorial/request-files/)
- [openpyxl workbook parsing and XML security](https://openpyxl.readthedocs.io/en/stable/)
