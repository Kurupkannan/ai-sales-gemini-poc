# AI Sales — Plivo + Gemini Live

FastAPI backend for organization-authorized **outbound phone calls** through Plivo, with Gemini Live providing the conversation and spoken audio. Firebase protects application APIs; Supabase is used as PostgreSQL. The UI supports individual lead entry, lead imports, recorded calling permission, starting/stopping AI calls, and automatic call-status history.

## Start here

1. Install dependencies: `uv sync --group dev`.
2. Configure `.env` using `.env.example`; keep `OUTBOUND_CALLS_ENABLED=false` until ready.
3. Follow [database setup](docs/SUPABASE_SETUP.md) and run `uv run alembic upgrade head` using migration credentials. The Plivo migration is `0005`.
4. Follow [identity setup](docs/AUTH_SETUP.md) for Firebase, organization membership and an eligible contact.
5. Follow [Plivo setup](docs/PLIVO_SETUP.md) for the purchased number, backend secrets, HTTPS/WSS and a controlled test call.
6. Run `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 65536 --ws-max-queue 16`.

## Call flow

```text
Authorized client -> POST /api/orgs/{org_id}/calls -> Plivo dials customer
Customer answers -> Plivo POST /plivo/answer/{attempt_id} -> Stream XML
Plivo WSS /plivo/media -> PCM 8 kHz -> Gemini PCM 16 kHz
Gemini PCM 24 kHz -> PCM 8 kHz -> Plivo playAudio -> customer
Hangup/status callbacks -> persistent call state and cleanup
```

The answer webhook belongs to an **outbound** call. Incoming calls are unsupported. The backend includes signed callbacks, single-use media authorization, contact suppression checks, organization limits, interruption handling, provider hangup on bridge failure, and a database-backed reconciler. No external worker service is needed for telephony: the reconciler runs in FastAPI's lifespan, with database leases across processes.

The live voice is Gemini's `VOICE_NAME`; a separate Google Cloud TTS API/key is not used. The legacy standalone Edge TTS helper is not part of active playback; `TTS_MODE` does not select the live call voice.

## Files

- `app/api/calls.py`: authorized create/read API.
- `app/auth/calls.py`: attempts, contact eligibility, grants and lifecycle persistence.
- `app/plivo/`: async Voice API client, signatures, XML callbacks and media protocol.
- `app/telephony/`: orchestration, Gemini bridge and reconciliation.
- `app/audio/converter.py`: stateful streaming PCM conversion.
- `app/gemini/`: Gemini connection and agent prompt.
- `migrations/versions/0005_plivo_outbound.py`: additive call correlation migration.

## Tests

```sh
uv run pytest -q
```

Tests do not place calls. PostgreSQL tests use explicitly configured disposable databases (see the test module headers); never point test database variables at production. Live provider signature delivery, carrier audio quality and latency must be verified with the configured number before broad rollout.

`GET /health` is a liveness check. Enabling calling validates required configuration at startup. For a read-only database/schema check, run `uv run python -m scripts.check_database`.
