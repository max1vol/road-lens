# RoadLens HTTP API

RoadLens is an independent workflow demonstration. A durable receipt means a report was saved to RoadLens for review; it does not mean a council accepted the report.

The Site serves the routes below under `/api`. Its mutable state is D1. Python agents and the classifier run in the separate private Modal service. All responses use `Cache-Control: no-store`.

## Credentials and request limits

| Role | Credential | Permitted routes |
|---|---|---|
| Public | None | Health, identity, public evidence and report feed |
| Device | `Authorization: Bearer <device token>` | Device preparation, cancellation and submission |
| Officer | ChatGPT session matching `ROADLENS_OWNER_USER_ID` | Officer questions, private case details, retries and review changes |
| Internal worker | `Authorization: Bearer <internal token>` | Job claim, renewal and result callbacks |
| Site → Modal | Separate service bearer | Private Modal preparation and wake |

Device and internal credentials do not grant officer access. Officer POST requests must include an `Origin` equal to the Site origin. Browser provider keys are never used.

POST bodies require `Content-Type: application/json`. The default body limit is 16 KiB; internal job results allow 512 KiB. Rate limits per minute are 12 preparations per device, 30 submissions per device and 15 officer questions per authorized user. Rate-limit errors are HTTP 429.

Schemas originate in `backend/models.py`; generated TypeScript validators live in `contracts/generated.ts`. Pydantic model validators also enforce relationships such as total transcript length and valid evidence query scope. The private service OpenAPI document is `private-service.openapi.json`.

## Public routes

| Method and path | Response |
|---|---|
| `GET /api/health` | `{ "status": "ok" }` after a database check |
| `GET /api/identity` | `{ "signed_in": boolean, "user_id": string|null, "owner": boolean }` for the current session |
| `GET /api/public/evidence` | Bounded precomputed official metrics, map points, reference junctions, selected local context, source notes and data hashes |
| `GET /api/public/reports?after=<cursor>` | `{ "reports": PublicReport[], "cursor": integer, "has_more": boolean }` |

Start the report feed at cursor `0`. Each page covers at most 100 ordered change events; reports are deduplicated within that page. Merge cards by `report_id`, advance only to the returned cursor, drain immediately while `has_more` is true, then poll every two seconds. A report may appear again when its analysis or review state changes. Cursors must be non-negative safe integers.

`PublicReport` contains only report ID, category, declared junction/reference point, received time, source label and workflow states. It excludes arbitrary resident observations, transcripts, contact information and private run traces. Official map coordinates and the report reference point do not establish current street conditions.

## Device preparation and submission

### `POST /api/device/prepare`

Body: `PrepareRequest`.

```json
{
  "session_id": "opaque-session-id",
  "turns": [
    {
      "turn_id": "unique-resident-turn-id",
      "text": "The resident's exact words",
      "timestamp": "2026-09-19T17:00:00.000Z",
      "role": "resident"
    }
  ]
}
```

Accepts 1–30 chronological resident turns with unique IDs and at most 4,000 characters including newline separators. Timestamps include a timezone and may not be more than five minutes in the future. Device identity is derived from authentication, never supplied in the body.

A new preparation supersedes older pending/ready drafts in the same device session. Modal returns a typed `IntakeResponse`:

- `output.kind = needs_clarification`: `missing_fields` and one `question`.
- `output.kind = out_of_scope`: a short `message`.
- `output.kind = ready_for_confirmation`: a validated `ReadyDraft`; the Site additionally returns `draft_id`, `prepared_at`, `expires_at` and exact `readback`.

Every response includes the private `run` summary. A ready draft expires 15 minutes after it is stored. Preparation never creates a submitted report. A superseded ready preparation returns HTTP 409.

### `POST /api/device/drafts/<draft_id>/cancel`

Body: `{}`. Cancels an owned pending/ready draft and returns `{ "cancelled": true }`. It never submits or removes a received report. This acknowledgement is deliberately idempotent and does not reveal another device's draft.

### `POST /api/device/reports`

Header: `Idempotency-Key`, 16–128 characters from letters, digits, `_`, `.`, `:`, `-`.

Body: `Submission`.

```json
{
  "draft_id": "server-issued-draft-id",
  "confirmation": {
    "resident_turn_id": "new-resident-turn-id",
    "text": "Yes, please submit it.",
    "confirmed_at": "2026-09-19T17:00:10.000Z"
  }
}
```

The confirmation must be affirmative, use a turn ID absent from preparation, and occur after the draft was prepared. The device adapter also verifies that the exact server readback was spoken before accepting confirmation.

The Site atomically commits one report, its processing job, a receipt and a feed event. Response:

```json
{
  "report_id": "opaque-report-id",
  "received_at": "2026-09-19T17:00:11.000Z",
  "analysis_status": "queued",
  "review_status": "awaiting_officer_review"
}
```

HTTP 201 means newly committed; HTTP 200 returns a stored receipt for the same canonical body/key. Receipts are checked before draft expiry, so a retry can recover an already committed report after expiry. Reusing a key for different content returns 409. A consumed draft cannot create a second report under another key.

On an uncertain network result the box keeps the exact confirmed body and key in its local outbox, retries that same request and says it has not yet received confirmation. It reports successful storage only after a durable receipt. Receipt state describes initial acceptance; the public feed supplies current processing state.

## Officer routes

| Method and path | Body | Response |
|---|---|---|
| `POST /api/officer/ask` | `OfficerQuestion`: `{ "question": string, "report_id": string|null }` | HTTP 202 `{ "job_id": string, "state": "queued" }` |
| `GET /api/officer/jobs/<job_id>` | None | `{ "job_id", "state", "result": AnalysisResult|null, "error": string|null }`; question job must belong to this owner |
| `GET /api/officer/reports/<report_id>` | None | Sanitized card plus private confirmed `report`, `intake_run`, `analysis` and `error` |
| `POST /api/officer/reports/<report_id>/retry` | `{}` | `{ "retry_requested": true }`; failed analysis is requeued with a fresh retry budget |
| `POST /api/officer/reports/<report_id>/review` | `{ "status": "reviewed" }` or `{ "status": "archived" }` | `{ "review_status": ... }` |

Questions contain 3–2,000 characters. An optional report ID provides the saved observation and resolved location as context. Agent output consists of evidence references, fixed limitations, inspection checks and (for a report brief) the attributed resident concern. Counts are rendered from code-computed metric objects with geography, period, unit and source.

`analysis_status` is `queued`, `running`, `complete` or `failed`. `review_status` is independently `awaiting_officer_review`, `reviewed` or `archived`. Agent callbacks never modify officer review state. A failed analysis leaves the received report visible.

## Private worker protocol

These endpoints require the internal bearer and never redirect into browser sign-in.

### `POST /api/internal/jobs/claim`

Body: `{}`. Atomically claims one queued job or expired lease with fewer than three attempts. Returns `{ "job": null }` when there is no claimable work, otherwise:

```json
{
  "job": {
    "id": "opaque-job-id",
    "kind": "officer_question",
    "payload": { "question": "What evidence is available for Cambridge in 2025?", "report": null },
    "lease_token": "opaque-lease-token",
    "attempts": 1,
    "lease_expires": "2026-09-19T17:02:00.000Z"
  }
}
```

The other kind is `report_analysis`, whose payload contains the complete confirmed `ReadyDraft` in `report`. Lease duration is 120 seconds. Expired third attempts become failed, preserving their reports.

### `POST /api/internal/jobs/<job_id>/renew`

Body: `{ "lease_token": string }`. Extends only the matching active lease. Response: `{ "renewed": true, "lease_expires": ISO8601 }`. Expired or superseded leases return 409. Workers renew every 20 seconds.

### `POST /api/internal/jobs/<job_id>/result`

Body: `JobResult` with `version: 1`, `lease_token`, and exactly one non-null outcome:

- `result: AnalysisResult`, `error: null`; or
- `result: null`, `error: "analysis_unavailable"`.

An active matching lease is required. A successful result returns `{ "accepted": true, "state": "complete" }`; an identical completed callback returns `{ "accepted": true, "duplicate": true }`. Errors requeue attempts below three, otherwise mark failed. Stale leases cannot replace a newer result and return 409.

The worker has a 90-second analysis deadline. An authenticated Modal `/wake` spawns a worker promptly. A minute reconciler invokes the same claim protocol to recover lost wakes and expired leases. Callback origins come only from fixed private configuration.

## Private Modal service

This service is not a browser or device API. Its bearer is `ROADLENS_MODAL_SERVICE_TOKEN`, distinct from the worker's internal Site credential.

| Method and path | Authentication | Behavior |
|---|---|---|
| `GET /health` | None | Structural service readiness |
| `POST /prepare` | Service bearer | `PrepareRequest` → `IntakeResponse`; no D1 writes |
| `POST /wake` | Service bearer | HTTP 202 `{ "accepted": true, "call_id": string }`; no callback URL accepted |
| `GET /openapi.json` | Service bearer | Generated private service contract, with declared `ServiceBearer` security |

The service authenticates before parsing the bounded request body or invoking inference. Invalid service input may produce FastAPI HTTP 422; the Site translates its own contract errors to 400. `/prepare` failures return 503 and do not create a received report.

## Common Site errors

| Status | Meaning |
|---|---|
| 400 | Invalid JSON, schema, cursor or request field |
| 401 | Missing or invalid credential/session |
| 403 | Signed-in user is not the allowlisted owner, or mutation Origin does not match |
| 404 | Unknown route/resource or inaccessible owned resource |
| 409 | Conflicting key, cancelled/expired/consumed/superseded draft, invalid confirmation or stale lease |
| 413 | Payload exceeds the route's bound |
| 415 | JSON content type required |
| 429 | Per-device/owner rate limit |
| 503 | Storage, provider or service unavailable; preserve retryable confirmed submissions |

Site errors return `{ "error": "..." }`. Provider exception bodies, secrets and hidden model reasoning are not exposed.
