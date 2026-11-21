# API and call latency investigation

Measured on 2026-10-12 from the local development machine against the configured
database and Gemini. No phone calls were placed. Existing application changes
were preserved. These are small diagnostic samples, not production percentiles.

## Plan and findings

1. Separate dial/API latency from pickup-to-first-audio latency.
2. Measure connection acquisition, transaction startup, SQL round trips and query execution.
3. Reduce measured round trips and overlap independent provider startup work.
4. Verify authorization, call limits, audio buffering and frontend behavior.
5. Capture a real call timeline to verify carrier playback and any remaining pickup delay.

| Probe | Observed |
| --- | --- |
| Local `/health`, warm | 0.8–1.1 ms client-side; 0.2–0.3 ms server after instrumentation |
| Cold database connection acquisition | 5,062 ms initially; 5,575 ms on repeat |
| Warm database connection acquisition | about 322–343 ms, includes pre-ping; not a pure pool-queue measurement |
| First `SELECT 1` in a transaction | about 644–684 ms, includes transaction startup |
| Subsequent `SELECT 1` in same transaction | 322–323 ms |
| Transaction rollback | about 322–343 ms |
| Original three dial-limit queries | 1,040.9 / 965.7 / 971.7 ms |
| Consolidated dial-limit query | 323.3 / 322.5 / 328.4 ms; identical aggregate results |
| Consolidated query server execution | 0.075 ms; planning 0.116 ms (`EXPLAIN ANALYZE`) |
| Gemini Live connect | 1,233 ms |
| Greeting request to first Gemini audio | 691 ms |
| Total Gemini startup | 1,926 ms, one sample |
| Optimized lead-list handler, warm transaction | 657 / 631 / 632 ms; excludes authentication, acquisition, tenant setup and rollback |

The configured pooler hostname identifies `ap-southeast-2` (Sydney). The measured
database transport/connection overhead dominates SQL execution in the sampled
call-limit query. This does not prove the database caused silence after pickup:
the current live answer, media admission and heartbeat paths use memory rather
than PostgreSQL. Live Gemini audio also bypasses the separate TTS engine.

## Changes made

- Start Gemini greeting preparation after authorization commits and wait for
  the first buffered PCM audio before dialing (updated October 16). Preserve
  prepared audio for uncertain dial results; stop preparation on definite rejection.
- Record monotonic milestones for dialing, answer, media connection/start,
  Gemini connection/greeting/first audio, and first audio sent to Plivo.
- Reuse bounded Plivo/Meta HTTP pools and close them on shutdown. Call creation
  still has no automatic retry. Close each Gemini async client after its session.
- Combine contact/preference/lead reads and the three rate checks: four fewer
  authorization round trips, with the organization lock and existing limits intact.
- Read existing users before attempting insertion; join organization and membership
  checks. Preserve per-request revocation, account suspension, role safety and tenant checks.
- Reuse status aggregates for exact lead counts when no text search is requested.
- Remove the duplicate dashboard lead-list fetch; use the table response for statistics.
- Serialize call-history polling, slow idle polling to 30 seconds, skip hidden tabs,
  and reload lead details only when completed-call state/summary changes. Recently
  ended calls continue fast polling while summaries are pending (up to two minutes).
- Move import row normalization to the thread pool so it does not block the audio
  event loop. Keep database transactions and import locking intact.
- Correct the database-status revision check from 0005 to the existing head 0006.

## Timing output

HTTP responses now include `X-Request-ID` and `Server-Timing`: `app`, `auth`,
`sql`, `db_transaction`, `authorize_call`, `plivo` and `meta` when applicable.
`greeting_prepare` now measures the wait before dialing. Its limit is
`CALL_GREETING_PREPARE_TIMEOUT_SECONDS` (default 10). Preparation failure marks
the attempt failed with `greeting_unavailable` without sending a Plivo dial request.
`greeting_ready` precedes `dial_start` in the call timeline. This shifts model
startup to before ringing; it does not eliminate carrier/media connection delay.
`api_timing` logs include the route template, SQL count and full request duration.
SQL timings include driver/network execution, not just PostgreSQL computation;
transaction timings also include acquisition and commit. These stages overlap
and must not be added together. SQL timing/count currently cover successful
cursor executions; a failed query is reflected in total request/transaction time.
Headers are captured at response start; final logs also cover response completion.
Bodies, tokens, SQL text/parameters and URL query strings are not logged by the
new instrumentation.

`call_timing` logs correlate stages by attempt ID and show `answer_elapsed_ms`.
Subtract milestones to distinguish:

- answer → media start: callback, tunnel/provider and WebSocket setup;
- Gemini connection start → first audio: model startup;
- media start → first audio sent: remaining preparation and bridge delay.

`first_audio_sent` means delivery to Plivo's socket, not confirmed audible playback
on the callee's handset. A real call is needed to measure the latter. No live
authenticated endpoint or real dial p50/p95 is claimed by this investigation.

## Repeat measurements

From `be`, with the existing server running:

```sh
.venv/bin/python -m scripts.measure_latency --samples 3
# Optional: makes one Gemini greeting request; never places a phone call.
.venv/bin/python -m scripts.measure_latency --samples 3 --gemini
```

The DB probes are reads only, and the script prints timings rather than records
or credentials. In the browser Network panel, reproduce the slow authenticated
request and inspect `Server-Timing` and `X-Request-ID`. For the next authorized
test call, compare the matching `call_timing` milestones with when audio is heard.

## Infrastructure follow-up

Run the API near its database and repeat the same probes before changing pool
size or adding indexes. A larger pool cannot remove the measured per-round-trip
delay. The database's primary region is fixed; moving it requires a separate
migration. No deployment, region, credentials, live schema or pool size was changed.
Keep a single API process for now: the current live-call registry and prepared
audio are process-local, so adding workers is not a safe speed fix.

Sources: [Supabase regions](https://supabase.com/docs/guides/platform/regions),
[database connection options](https://supabase.com/docs/guides/database/connecting-to-postgres),
[regional database latency guidance](https://supabase.com/docs/guides/functions/regional-invocation),
[SQLAlchemy pre-ping](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic).

## Validation

Final backend run: 173 passed, 9 skipped (other explicitly configured integration
suites), with one existing Starlette/httpx deprecation warning. The disposable
PostgreSQL container was stopped after verification.

Frontend production build and 34 frontend tests passed using the bundled Node
runtime (the system Homebrew Node has a missing shared library). Authorization
and call-state integration tests run on a new disposable Docker PostgreSQL 17
instance, with a restricted runtime role, never the configured live database.
One existing test passed a contact ID to a lead-history API; corrected it to use
the lead ID. Added coverage for early greeting preparation, rejected/uncertain
dials, timing isolation/redaction, HTTP client reuse, and list count equivalence.

## Follow-up: 10-second database-stage 503s

User logs later showed `db_transaction` at 10,002 ms with zero completed SQL
statements. Both connection and pool timeouts were configured to 10 seconds;
those timing logs alone cannot distinguish the two. A fresh connection succeeded
in 4.94 seconds, while the existing API process held five database sockets.
This supports a pool/stale-network failure hypothesis, not proof of a slow query.

Added a per-driver-operation network deadline (default 20 seconds), covering
queries and pool pre-pings. On timeout the socket is closed before cancellation,
so failed connections release pool capacity without waiting indefinitely for a
network cancellation response. New connections retain the 10-second connect
timeout. No business transaction or call creation is automatically retried.
Pool pre-ping may reconnect safely before the business transaction starts.
Connections recycle after 300 seconds, with TCP keepalive settings for failed
network detection. `db_connect` timing measures new connection establishment.

Safe `database_error` logs now include exception class, SQLSTATE, invalidation,
pool occupancy and request ID, but not exception text or SQL parameters. The next
failure will distinguish `pool_timeout` from a driver/database operation failure.

Firebase uses an explicit 10-second HTTP timeout (per attempt; SDK retries may
take longer). Recognized provider/certificate transport outages return 503 rather
than incorrectly reporting an invalid login. Revocation checking remains enabled.

Recovery tests use a local TCP proxy that silently drops packets on an established
PostgreSQL connection. They check both an in-flight query and a pool pre-ping,
then verify that the next request succeeds with a replacement connection. Live
authenticated `/api/me` recovery still requires a request from the signed-in user.
