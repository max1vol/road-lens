# Evaluation contract

All cases here are authored task specifications. The numerical answers come from actual source records. None of these are real resident feedback or an executed result of the future application.

- `intake_cases.json`: 20 model cases. Evaluate preparation only. `ready_for_confirmation` is not permission to submit. There are no pre-existing stored reports. Structured gold labels are initial human-reviewable proposals; freeze them before evaluation.
- `evidence_cases.json`: 12 model questions with independently checked numerical expectations and executable SQL oracles. The oracle SQL is trusted local test code, never an endpoint that accepts user-authored SQL. Agent output must match value, unit, source and period. A number alone does not pass.
- `behaviour_cases.json`: eight system scenarios. Some require integration tests and others a grounded-response rubric. Authentication/idempotency are common application guarantees, not differential AI-accuracy metrics.

Implement a `run_intake(case)`, `run_evidence(case)` and `run_behaviour(case)` adapter for the actual app. Use Pydantic Evals custom evaluators for field/source/claim outcomes and deterministic integration tests for state transitions. Store original model outputs and failures alongside the evaluated structured result.

Split policy: these 40 cases are proposed test fixtures. The eight sentences in `scripts/check_classifier.py` are a separate development smoke set. Freeze fixture hashes before tuning; do not tune prompts after examining scored test failures and then call the same cases held out. Add fresh, independently written cases if further tuning is needed.

Compare a competent plain Gemini SDK agent, Pydantic AI without classifier hints, and the full Pydantic AI + GLiNER system. Use the same model, data, output schema, tools and comparable budgets. Preserve common security/write checks in all arms. See the build brief for metric definitions and reporting rules.

`scripts/evaluate_classifier_smoke.py` is a small, executed example of the Pydantic Evals API; its saved development predictions are not answers to these 40 cases.
