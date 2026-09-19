# RoadLens deployment and handoff

## Release record

Status recorded on 19 September 2026. The existing Gemini Live hardware integration is working; temporary loss of Ethernet/SSH access does not establish a voice-box fault.

| Component | Recorded state |
|---|---|
| Public Site | https://roadlens-cambridge.max1-volovich.chatgpt.site |
| Source repository | https://github.com/max1vol/road-lens |
| Initial repository import | Connector commit `48ae251` contained local source snapshot `9682c5`; later releases are on the same main branch. |
| Scored evaluation source | Clean commit `f2c31c805d75af51885a66ce4ee2229161faa453`; deployed agent source at `8c3f250` is identical. |
| Sites project | `appgprj_6aaeb3a830708191a50d9d7fdb6bc739`; deployment metadata is in `.openai/hosting.json` |
| Modal application | `roadlens-cambridge` |
| Private Modal API | https://max1-volovich--roadlens-cambridge-api.modal.run (service bearer required) |
| Modal functions/class | `api`, `worker`, `reconcile`, `runtime_manifest`, private `development_smoke`; `Classifier.predict` and `Classifier.runtime_manifest` |
| Mutable state | Site D1 binding `DB` |
| Classifier weights | Modal Volume `roadlens-gliner-pinned-weights`, pinned model revision below |
| Existing physical voice runtime | User-confirmed working Gemini Live, one press starts hands-free conversation, another stops; 20 seconds without speech ends an idle conversation |
| New cloud adapter | Installed on the Pi, 19 September 2026; existing voice user service active and ready. |
| Pi connection | Direct Ethernet reachable at `pi@192.168.2.2`; trusted existing host key verified. |
| Officer authorization | Owner Site-specific user ID is not configured yet; requires the owner's real ChatGPT sign-in identity |
| New physical cloud rehearsal | **Not exercised**; no physical report/run IDs recorded |
| Authored agent comparison | A 34/35; B 35/35; C 34/35. All 105 attempts completed. See `research/evaluation-20260919T170203Z/` and fixture ambiguity in `EVALUATION.md`. |

Update the release record after final publication and verify the served Site, GitHub snapshot and deployed Modal code commit correspond. Earlier isolated test report IDs are not production or hardware evidence.

## Runtime contracts and credentials

[HTTP_API.md](HTTP_API.md) describes the Site and private service routes. Pydantic contracts live in `backend/models.py`; regenerate with `python scripts/export-contracts.py` when they change. `contracts/generated.ts` is generated from those models.

Use the existing private secret facilities; never commit their values or print them into deployment logs.

| Setting | Location and purpose |
|---|---|
| `ROADLENS_API_BASE_URL` | Pi private environment; public Site origin |
| `ROADLENS_DEVICE_TOKEN` | Pi private environment; prepare/submit/cancel only |
| `ROADLENS_DEVICE_TOKEN_SHA256` | Site secret; digest of the device bearer |
| `ROADLENS_DEVICE_ID` | Site configuration; server-resolved device identity |
| `ROADLENS_MODAL_SERVICE_TOKEN` | Site secret and Modal Secret; Site-to-agent service authentication |
| `ROADLENS_MODAL_URL` | Site configuration; deployed private Modal `api` endpoint |
| `ROADLENS_INTERNAL_TOKEN` | Site secret and Modal Secret; claim/renew/result callbacks |
| `ROADLENS_SITE_ORIGIN` | Fixed Modal callback origin, and matching Site configuration |
| `GEMINI_API_KEY` | Existing box key file and Modal Secret; never browser code |
| `ROADLENS_OWNER_USER_ID` | Site configuration; explicit officer allowlist |
| Modal deployment token pair | Deployment environment only; never application/client credentials |
| `ROADLENS_CODE_COMMIT` | Modal image build environment; actual source commit for trace provenance |

The shared Modal Secret is named **`roadlens-service`**. Its application values include `GEMINI_API_KEY`, `ROADLENS_MODAL_SERVICE_TOKEN`, `ROADLENS_INTERNAL_TOKEN` and `ROADLENS_SITE_ORIGIN`.

To finish officer configuration, sign in through the Site's ChatGPT sign-in UI using the intended owner's account. Read `/api/identity` in that signed-in session, configure that exact `user_id` as `ROADLENS_OWNER_USER_ID` through Sites server environment settings, then deploy and verify `owner: true`. Another signed-in account must remain denied. Never copy the device or internal bearer into this setting.

## Build and publish

Use the existing Sites project and its native build/save/deploy flow. Do not deploy this checkout to another hosting provider or substitute a separate Cloudflare account. Keep the existing D1 binding and migrations; do not recreate or clear the production database.

From the checkout root:

```text
npm run install:ci
python scripts/export-contracts.py
npm run db:generate
npm run build
```

Inspect any generated migration before publishing. `scripts/build-verified.sh` is the bounded build wrapper when the selected Sites execution profile supplies GNU `timeout`. The project includes its supported runtime helpers under `scripts/` and `.sites-runtime/`.

For Modal, use a Python 3.12 deployment environment with `modal==1.5.5`, ensure its configured profile is the intended account, and set `ROADLENS_CODE_COMMIT` to the actual reviewed source commit. Agent/classifier dependencies are installed inside their separate Modal images; a deployment laptop does not need the Linux-only CPU classifier stack. Deploy with:

```text
modal deploy -m backend.modal_app
```

The production service, workers and minute reconciler are defined in `backend/modal_app.py`. Worker leases are 120 seconds, renewed every 20 seconds; each analysis has a 90-second deadline and at most three attempts per job before a visible failure. A report remains received while its analysis is queued, running or unavailable. A later owner retry starts a fresh bounded attempt budget.

The classifier is `fastino/gliner2.5-base-v1`, revision `78cea040597df251eedefa9d7ee2a756af39fe64`, loaded with **`AutoExtractor`** from a pinned local snapshot. It uses four CPU threads, four allocated CPUs and 8 GiB RAM, with one inference at a time per container. Its weights are separate from the slim agent image. Mutable SQLite is not stored on the weights Volume.

A private development smoke may be invoked through Modal RPC for diagnostics. It uses disclosed authored examples, creates no Site report, and is not a held-out evaluation or physical rehearsal. Allow the smoke enough time for its two sequential bounded agent runs. Do not increase production budgets merely to hide a failed validator; inspect observable tool calls and repair messages first.

Resolved Linux runtime records are in `research/runtime/agent-linux.json` and `research/runtime/classifier-linux.json`; corresponding dependency locks are `backend/requirements-agent-linux.lock` and `backend/requirements-classifier-linux.lock`. Preserve the official CPU wheel source for the pinned `torch` CPU build. Refresh these manifests after any environment change.

## Install the cloud adapter without replacing the working voice stack

Follow [box_adapter/INTEGRATION.md](../box_adapter/INTEGRATION.md). That document explains the precise patch, private environment file, readback gate, durable outbox and synthetic replay commands.

1. Restore Ethernet/SSH reachability and inspect the actual running service and files. Do not reinstall the OS, change audio/GPIO configuration or replace the working Gemini Live setup.
2. Back up the current `roadlens.py`, `aiy_gemini.py`, user service/drop-ins and private state **on the Pi**. Retain the prior runtime and original working files.
3. Integrate the staged `roadlens.py` and the small `aiy_gemini.patch` into that inspected version. Use the full staged voice file only if its base matches the real installed source. Run the regression checks before restarting.
4. Create `/home/pi/.config/roadlens/` with mode `0700` and a `0600` service environment file containing only the cloud Site origin and device bearer. Add an `EnvironmentFile=` systemd user drop-in; preserve the current Gemini key and service launch configuration.
5. Reload the user service configuration and restart only `aiy-gemini-live.service`. Check its ready state, then rehearse the flow in [DEMO.md](DEMO.md) with the real microphone and speaker.

The cloud adapter uses the Python standard library; no Pydantic AI, GLiNER or heavyweight classifier installation is required on the Pi. It preserves `gemini-3.8-live-extended-thinking`, existing thinking settings, PCM formats, GPIO handling, echo suppression, one-press behavior and the 20-second speech inactivity timer. The original HAT has no acoustic echo cancellation: the existing microphone suppression during playback remains. Do not claim new acoustic barge-in support.

The authoritative inbox for new submissions is the public Site. The old local dashboard reads the earlier local prototype database and does not prove cloud receipt.

## Rollback

If the cloud adapter causes a regression, stop the voice user service, restore the immediately preceding backed-up voice/adapter files and service configuration, reload the user service manager and restart the original working command. This returns the box to its prior Gemini Live mode; it does not require reverting its OS or hardware configuration.

Do **not** delete the private confirmed-submission outbox. A network-uncertain submission may already have been committed; preserve its exact body/key and reconcile its original receipt before attempting any new submission. Restoring voice code must not be described as cancelling or deleting a received cloud report.

For a Site or Modal regression, deploy the previous recorded version through that platform's supported flow. Preserve D1 and its records; review migration compatibility before changing application versions. Reducing classifier warm capacity after the demo may return it to scale-to-zero without deleting its weights Volume.

## Evidence recorded so far

| Check | Result and scope |
|---|---|
| Workflow behavior | **5/5** against actual API routes and isolated local D1; refusal, retries past expiry, key conflict, authentication and worker recovery. Callback failures/expiry were simulated, not an actual Modal outage. See `research/workflow-results.json`. |
| HTTP races/feed | **5 unittest methods, 11 recorded fixtures passed**; concurrent retry conflicts, duplicate completion and paged feed updates against a disposable loopback database. See `research/http-race-results.json`. |
| Backend checks | **14 pytest and 21 unittest checks passed** at this handoff; these establish code/contract behavior, not model benchmark accuracy. |
| Box adapter regressions | **43 checks passed** with mocked HTTP/audio; see the adapter integration guide. |
| Physical Gemini Live | Existing working setup confirmed by the user before this cloud patch. |
| Pi cloud adapter | Installed; 43 checks passed on the Pi. Real HTTPS health and authenticated prepare passed. Physical spoken rehearsal awaits user feedback. |
| Real agent comparison | Completed once: A 34/35, B 35/35, C 34/35. Median latency 2.95s / 2.70s / 3.09s. GLiNER did not improve this run. |

## Synthetic fallback and final verification

With private environment configuration loaded, use either:

```text
python box_adapter/cloud_smoke.py --submit --mode api
python box_adapter/cloud_smoke.py --submit --mode live
```

**Both create a real cloud report from authored synthetic input. Neither opens the physical microphone or speaker.** API mode checks actual preparation/submission and the public processing feed. Live mode additionally exercises the real Gemini WebSocket and adapter with synthetic turns; returned PCM is counted/hashed and discarded. Without `--submit`, the script only explains its purpose and makes no network calls.

Each run writes private `0600` result/envelope files beneath a fresh `0700` directory in `~/.local/state/roadlens-smoke/`. Preserve an uncertain confirmed envelope for an exact-key retry. A saved report whose analysis fails is a received report with failed analysis, not a successful completed rehearsal.

Final release checks still requiring recorded evidence are: final published source/version alignment; anonymous public access; intended owner authorization; the physical report → durable receipt → browser arrival flow; cancellation and lost-response recovery on the real setup; and actual physical report/intake/evidence run IDs. Report any unexercised gate explicitly.

## Executed cloud replay

`research/cloud-api-rehearsal.json` records an authenticated API replay using authored slide wording, real Gemini/GLiNER preparation, D1 storage, and the real Modal evidence worker. It passed at 17:04 UTC on 19 September 2026. Report `89dcd199-0122-4768-96a0-cca078c7a28d` progressed queued → running → complete and remained `awaiting_officer_review`. This used no physical microphone or speaker and is explicitly synthetic.

The device HTTP client identifies itself honestly as RoadLens because the public edge rejects urllib's generic default signature. TLS verification remains enabled. The automated replay waits one second before its separate confirmation turn to avoid normal subsecond clock differences; physical confirmation already follows completed playback and a new resident response.

During integration, rolling Modal deployments temporarily served an earlier container revision. The final source must be verified with the private `runtime_manifest` (commit and agent-source SHA-256); `modal deploy --strategy recreate -m backend.modal_app` replaced those stale containers during the empty-queue integration window. Do not terminate active report processing without accounting for its leases.

## Pi installation record

Installed 19 September 2026 at 18:06 BST. The existing `aiy-gemini-live.service` returned active/running with “Ready: press once to talk; standby after 20 seconds of silence.” The original source was verified against the patch base before installation. Backup: `/home/pi/aiy-gemini-live/backups/pre-cloud-20260919T170437Z-17c772`. Preflight/tests: `/home/pi/aiy-gemini-live/check-cloud-20260919T170437Z-17c772`. The private cloud environment and durable outbox have mode `0600`. Gemini Live extended thinking MEDIUM, GPIO 23/25, and original HAT ALSA configuration remain unchanged. No microphone or speaker test was inferred from the automated checks.
