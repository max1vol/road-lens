# RoadLens

A Raspberry Pi voice box turns a confirmed resident observation into a durable report, with dated Cambridge road-safety evidence for officer review.

## Architecture

- `app/`, `components/`, `db/`, `lib/`: Sites React/TypeScript dashboard, authenticated routes and D1 state.
- `backend/`: Pydantic AI intake/evidence agents, immutable SQL evidence repository, pinned GLiNER classifier and Modal worker.
- `box_adapter/`: small cloud integration for the working AIY box; button/audio/20-second silence behavior preserved.
- `data/`: pinned Cambridge-only evidence and computed map facts. National final and local provisional sources remain separate.
- `contracts/`: schemas/types/runtime validators generated from Python models.
- `evals/`, `tests/`, `research/`: frozen cases, checks and measured results.

The app is an independent hackathon demonstration. Saved reports go to RoadLens for human review, not a council inbox. No audio is stored by default. Arbitrary resident text remains private.

## Development

Python 3.12. Install backend/requirements.txt in a virtual environment. Run `python scripts/export-contracts.py` after contract or evidence changes. The Site uses its locked npm dependencies, `npm run dev`, `npm run build`, and generated Drizzle migrations via `npm run db:generate`.

Application secrets belong in a private environment file or platform secret storage. See docs/BUILD_BRIEF.md for the role-separated credentials and acceptance criteria. Never commit Gemini keys, device tokens, Modal credentials or resident transcripts.

The public dashboard is https://roadlens-cambridge.max1-volovich.chatgpt.site. Release status and configuration are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md); the three-minute rehearsal is in [docs/DEMO.md](docs/DEMO.md).

Measured comparison: plain Gemini 34/35, Pydantic AI 35/35, Pydantic AI + GLiNER 34/35 on one fixed authored set; all 105 attempts completed. See [results and limitations](docs/EVALUATION.md). GLiNER did not improve the score in this run. Separate infrastructure checks passed 5/5 workflow scenarios and 11/11 concurrency fixtures. Physical rehearsal status is recorded separately and must not be inferred from these tests.
