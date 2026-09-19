# Cloud device adapter handoff

The existing Raspberry Pi hardware, Gemini Live connection, one-press conversation and 20-second silence timeout are preserved. This cloud reporting adapter was installed on the Pi on 19 September 2026 after verifying the original source and passing all 43 checks in its existing Python environment. The existing voice service is active and ready. Installation paths, private backup and the separate physical rehearsal status are recorded in `docs/DEPLOYMENT.md`.

The directory contains a replacement `roadlens.py`, the original working `aiy_gemini.py` with a small integration patch, and tests. Integrate the cloud changes around the existing working voice flow; preserve its model, audio handling, button control and silence timeout.

## Files to integrate

- Replace the Pi application's `roadlens.py` with this copy.
- Apply `aiy_gemini.patch`, or use the included complete `aiy_gemini.py`. The patch adds output transcription, forwards actual resident and assistant transcripts, gates readback completion before the next microphone frame, clears interrupted readback evidence, and runs a background outbox retry task during the normal voice service.
- Copy `test_cloud_adapter.py` and `test_cloud_voice.py` into the project's tests directory. `test_cloud_voice.py` imports the existing `test_voice_box.py`; the copy in this directory is unchanged from the original suite.

The Gemini model, thinking setting, PCM formats, audio commands, GPIOs, button semantics, echo suppression and 20-second speech inactivity timer are unchanged. The original HAT still suppresses microphone capture during playback because it has no echo cancellation. This change does not claim to add acoustic barge-in.

## Private configuration

The existing service needs these environment variables:

```text
ROADLENS_API_BASE_URL=https://<actual-site-origin>
ROADLENS_DEVICE_TOKEN=<generated-device-bearer>
```

Use a `0600` environment file under `/home/pi/.config/roadlens/` in a `0700` directory, and reference it using a systemd user service `EnvironmentFile=` drop-in. Do not put values in the source repository, browser bundle, command arguments or logs. Keep the existing Gemini key file and live service command. The cloud adapter does not read or send the Gemini key.

No additional Pi dependencies are required: the cloud adapter uses Python's standard library. The existing voice runtime needs `websockets==16.1.1` and `webrtcvad-wheels==2.0.14`, which are already in its original requirements.

## Contract

`prepare_road_report` takes `{}` only. The adapter sends `session_id` and up to 30 captured resident turns (`turn_id`, exact concatenated transcription chunks, first-chunk `timestamp`, `role='resident'`) to `POST /api/device/prepare`. Total text is limited to 4,000 characters and the encoded request to 16 KiB; oversized conversations fail visibly rather than dropping earlier corrections.

Expected ready response:

```json
{
  "output": {"kind": "ready_for_confirmation", "readback": "A visibility concern at Vicarage Terrace and St Matthews Street. Shall I submit that?"},
  "draft_id": "<server-id>",
  "prepared_at": "<ISO timestamp>",
  "expires_at": "<ISO timestamp>",
  "readback": "A visibility concern at Vicarage Terrace and St Matthews Street. Shall I submit that?"
}
```

Other `output.kind` values supported are `needs_clarification` and `out_of_scope`.

Use a naturally speakable server readback. The device matches its complete words against actual output transcription, ignoring case and punctuation. It also recognizes the supplied gazetteer's specific place-name alias: “St Matthews Street”, “St. Matthew’s Street” and “Saint Matthew’s Street” name the same street. This normalization applies only to that named street in the model's readback. It does not broadly expand “St”, fuzzy-match other roads, drop words, or alter resident confirmation matching. A literal slash pronounced as “and”, another changed word, or any omitted sentence can still fail the match. The speaker must also have received PCM, finished playback and its echo tail, and the model must be IDLE with no pending tool call. A new resident turn must then clearly affirm it. Interrupted playback clears incomplete readback evidence.

`submit_road_report` takes only `{draft_id}`. The device supplies the recorded confirmation (`resident_turn_id`, exact `text`, actual `confirmed_at`) and a generated `Idempotency-Key`. It commits this exact request to a private, fully synchronous SQLite outbox **before** making the first network call.

Only a 200/201 response containing `report_id`, valid `received_at`, and the documented analysis/review statuses produces the spoken saved acknowledgement. A timeout or uncertain outcome says that confirmation has not been received. The background task retries pending requests in standby and after a service restart, preserving the same body and key even after the draft expires. It never prepares or reclassifies during a retry. Explicit 4xx rejection blocks that request; expiry/conflict requires fresh preparation and confirmation.

Declines, corrections, replacement drafts and ended conversations invalidate unsubmitted drafts locally and queue `POST /api/device/drafts/<id>/cancel` with `{}` for server revocation. Confirmed outbox entries remain immutable; a network-uncertain submission cannot be pretended to have been cancelled.

## Verification

Run:

```text
python -m unittest -v test_cloud_adapter test_voice_box test_cloud_voice
```

The local suite has 29 adapter/transport checks, 12 unchanged voice regression checks and 2 integration checks: 43 total. It exercises confirmation chronology, actual-turn grounding, cancellation, malformed receipts, exact-body/key retries, crash/restart recovery past expiry, private file modes, redirects, unchanged timeout/echo/button behavior, and readback/playback/IDLE gating. The additional alias cases accept only the documented Saint/St Matthew’s/Matthews variants, reject other streets and changed questions, and preserve the fresh-confirmation and refusal checks.

These tests use mocked HTTP and audio. A physical box → deployed API → real inbox rehearsal remains required; no report ID or physical success is asserted by this handoff. Public reference for the output audio transcription setup: https://ai.google.dev/gemini-api/docs/live-api/capabilities#audio-transcriptions

The old local dashboard reads the previous local prototype report database. It is not the authoritative cloud inbox and should not be used to demonstrate new report storage. `state.json` remains available for sanitized box status, with no raw resident turns or device token.

## Synthetic cloud smoke / CLI replay

`cloud_smoke.py` is opt-in. Without `--submit` it only prints its purpose and makes no network calls. Its two explicit modes are:

```text
python cloud_smoke.py --submit --mode live
python cloud_smoke.py --submit --mode api
```

Run with the same private `ROADLENS_API_BASE_URL` and `ROADLENS_DEVICE_TOKEN` environment as the service. Live mode also reads the existing Gemini key file. The parent deployment workflow should load the private environment without echoing its contents. Neither invocation should run until the parent deliberately chooses to create a synthetic demonstration report.

**Both modes create a real cloud report. Neither is a physical hardware rehearsal.** Live mode opens the actual Gemini Live WebSocket, registers explicitly authored synthetic resident turns, executes the actual adapter/tools against the configured Site, verifies the exact model readback transcription with returned PCM, attaches a later synthetic confirmation, and receives a real server receipt. PCM is counted/hashed and discarded; microphone and speaker are never opened. Readback drain in this mode is a muted test sink, not evidence that a person heard the box.

API mode is the honest CLI fallback. It calls the real prepare/submit endpoints directly with authored synthetic turns and confirmation. It does not test Live, microphone, speaker, or the adapter's acoustic readback gate. It persists its exact confirmed request and key before sending and retries that same envelope for transient errors.

Both modes then poll `GET /api/public/reports?after=<cursor>` and succeed only when their actual `report_id` appears with `analysis_status='complete'`. A real worker failure or timeout is reported as failure while preserving the saved report ID. Private analysis contents are not exposed by that public route and are not inspected by this script.

Results are `0600` JSON files beneath a fresh `0700` directory in `~/.local/state/roadlens-smoke/`. The printed summary includes `synthetic:true`, `physical_rehearsal:false`, actual report/draft IDs, workflow status and PCM counts/hash where applicable. It contains no API token. The Live adapter's separate outbox remains inside that run directory, so a network-uncertain write can be retried with the original key rather than creating another report. This script has only been compiled and exercised without `--submit` in the isolated handoff directory; it has not been run against the Pi or live Site by this agent.

## Optional local HTTP concurrency tests

`test_http_races.py` uses the real local Site routes and the parent's `evals.workflow_contract.SQLiteProbe`. It is restricted to a loopback origin, ignores proxy environment settings, requires an empty disposable migrated D1 SQLite database, and verifies that the HTTP preview uses that exact DB via a random seeded draft cancellation. It never clears existing data and leaves its fixture rows for inspection.

Set `ROADLENS_TEST_ISOLATED=1`, `ROADLENS_TEST_ORIGIN`, `ROADLENS_TEST_DB`, `ROADLENS_TEST_DEVICE_TOKEN` and `ROADLENS_TEST_INTERNAL_TOKEN`. The local preview must have Modal disabled or use a harmless local fake wake endpoint, with background workers stopped. Set `ROADLENS_TEST_DEVICE_ID` if the server's device identity differs from its default `aiy-box-1`. `ROADLENS_TEST_PROJECT_ROOT` points to the checkout if it cannot be found from the working directory. `ROADLENS_TEST_RESULT_PATH` optionally writes a private machine-readable report.

The five unittest methods cover simultaneous identical retries; simultaneous same-key/different-valid-confirmation conflicts; consumed draft reuse under a different key; duplicate successful completion of one active lease; and 230 feed events spanning three pages, including 125 distinct reports and repeated events. Race pairs run three times by default (`ROADLENS_TEST_RACE_ROUNDS` allows 1–4). Tests assert one report/job/receipt/event per submission and one persisted completion event per lease.

The completion payload is a real Pydantic `AnalysisResult` containing an explicit infrastructure-only clarification, empty metrics/records, zero model usage, and `INTEGRATION_FIXTURE_NO_MODEL_EXECUTED` model identifiers. It is never presented as generated road-safety evidence. This agent verified that payload against the current production Python schema and checked the loopback guards, then confirmed the suite skips when the opt-in environment is absent. The parent subsequently ran all five methods against the isolated local API: 11 recorded fixtures passed, as recorded in the checkout's `research/http-race-results.json`. This is local API verification, not a new physical box rehearsal. These tests add no pytest dependency.
