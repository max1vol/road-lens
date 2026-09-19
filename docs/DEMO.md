# RoadLens — three-minute rehearsal

Open **https://roadlens-cambridge.max1-volovich.chatgpt.site**. RoadLens saves concerns for review; it is an independent demonstration, not a council reporting service.

**Current state:** the existing Gemini Live box is working and the cloud adapter is installed. Real cloud submission/analysis, a muted Gemini Live tool-loop check and the 105-attempt agent comparison have completed. The muted check used authored synthetic turns and no physical audio. Officer authorization still requires the owner’s Site-specific sign-in ID; the new physical spoken rehearsal awaits user confirmation.

Before rehearsing, follow [DEPLOYMENT.md](DEPLOYMENT.md) and [adapter integration](../box_adapter/INTEGRATION.md), verify cloud intake and owner access, and note existing inbox IDs so the genuine new arrival is distinguishable. Do not inject or erase reports for effect.

| Time | Action and narration |
|---|---|
| **0:00–0:25** | Show the map and scope strip. “National 2025 final data and the newer local provisional snapshot remain separate.” Point to **215 collisions** and **249 people injured**; neither count measures individual journey risk. |
| **0:25–0:50** | **Press once** and wait for “What road concern would you like to report?” Say **“I can't see past parked cars when I cross.”** Let the box ask which junction. Say **“Vicarage Terrace at St Matthews Street.”** Allow two seconds of silence after each answer. Press again to cancel. |
| **0:50–1:15** | Wait for the entire exact server readback and confirmation question. Say **“Yes, please submit it.”** Only a durable receipt permits the saved acknowledgement. Watch the real report arrive and progress; officer review stays pending. Without a receipt, say confirmation is pending. |
| **1:15–1:50** | Open the case. Separate the resident's present observation from local record **1737997**: **13 April 2026**, **Serious collision**, **two casualties**, **provisional**. This does not mean both casualties were serious. Show **“Current imagery not supplied.”** |
| **1:50–2:20** | Ask **“Show cyclists injured in Cambridge in 2025.”** Verify **121 people**, Cambridge district, January–December 2025, national final source. Open the provenance and query reference. |
| **2:20–2:45** | Ask **“Did the parked cars cause that collision?”** The evidence cannot establish causation. Show inspection checks for current sightlines; no works have been approved. |
| **2:45–3:00** | Open an actual run: model ID, real classifier output, tool calls, validation events and snapshot hashes. Show evaluation scores only after execution, with failures and denominators. |

**Hardware fallback:** introduce it explicitly as synthetic. With private configuration loaded, run `python box_adapter/cloud_smoke.py --submit --mode api`. It creates a real cloud report from authored text and waits for actual analysis. `--mode live` additionally tests the real Gemini WebSocket/adapter, with returned PCM discarded. **Neither mode tests the physical microphone or speaker.** See the integration guide for private artifacts and exact-key retries.

Also rehearse **“No, cancel it”** and verify no report appears. For a lost response, retry the same confirmed outbox body/key; do not prepare another report. Isolated HTTP tests cover these transitions but do not replace physical verification.

**Last physical cloud rehearsal:** not run. **Physical report/intake/evidence run IDs:** not recorded. Record them only from a successful hardware rehearsal, never from CLI replay or isolated fixtures.
