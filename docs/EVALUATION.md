# RoadLens evaluation

The frozen pack contains 40 authored scenarios: 35 model tasks and five common workflow scenarios. It is not real resident feedback, and independent human review of the proposed gold labels has **not** been recorded. The eight old classifier development examples are not included in the model-comparison denominator. Never describe the old 6/8 development result as this evaluation.

Model tasks use the real Pydantic Evals 2.46 `Dataset(name=...)`, `Case`, `Evaluator`, and `EvaluatorContext`. The custom evaluator recomputes every selected metric against the read-only SQLite snapshot. A correct number with a wrong unit, geography, source, period, filter, or reference fails. B05–B07 check typed source-separation, causal/exposure limitations, and absence of invented current-image claims. There is no prose-quality LLM judge.

Arms:

- A: a manual Google Gen AI SDK tool loop with Gemini `gemini-3.8-flash`.
- B: the production Pydantic AI builders, without classifier advice.
- C: the same production builders, plus a real call to the deployed Modal `Classifier.predict` for each intake.

All use the same instructions, output models, deterministic evidence functions, and domain validators. This conservative baseline does **not** disable safety. The comparison isolates orchestration and classifier advice; it must not attribute shared infrastructure safeguards to Pydantic. B and C share the officer-evidence path intentionally, since GLiNER preprocesses resident intake only. Budgets are six requests, eight ordinary tools, 2,500 output tokens (including provider-reported thinking tokens), and 90 seconds per task. Runs alternate the arm order by fixture to reduce time/load bias. Retries count against the budget and are recorded. A final-output schema repair is not a new independent example.

Run deterministic tests first, from the integrated repository:

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_evidence_contract.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_eval_harness.py' -v
.venv/bin/python -m evals.run_suite --validate-only
```

The tests execute all 12 supplied SQL oracles plus source, period, unit, age, radius, location, privacy, and evaluation checks. They make no provider calls. Do not edit the frozen cases after observing results. `research/frozen-inputs.json` is verified before inference and its initial human-review limitation is retained in the manifest.

Once the production agent/classifier smoke checks pass and the code is committed, execute one comparison. Read the Gemini key through the private environment; never put it on a command line or in result exports:

```sh
.venv/bin/python -m evals.run_suite --execute --output research/evaluation-UNIQUE-RUN-ID
```

For a Mac that cannot connect directly to Gemini, use the supplied Modal runner after integrating these files. It uses the existing `roadlens-service` secret and the actually deployed `roadlens-cambridge` classifier:

```sh
.venv/bin/modal run -m evals.modal_runner --smoke
.venv/bin/modal run -m evals.modal_runner
```

This creates an isolated ephemeral evaluation application, makes the same real requests, and downloads the raw JSON artifacts. It does not deploy a public endpoint. Run the default one repetition first. Three repetitions are optional; report repeated attempts grouped by fixture and never as more independent cases.

Results:

- `manifest.json`: fixture/data hashes, prompt/code hashes, actual model ID, package versions, code commit, source dirty flag, budgets, review status, and run times.
- `cases.jsonl`: authored input, expected output, actual output, visible model tool/final calls, classifier output, usage, validation repairs, latency, evaluator outcome, and safe failure metadata. Thinking parts, signatures, credentials, raw audio, and live resident transcripts are excluded.
- `summary.json`: pass numerator/denominator, completed attempts, latency, usage, failures, per-task results, and repeat grouping.
- `pydantic-evals-report.json`: actual Pydantic Evals assertion outcomes.

Model failures still enter the denominator. Transport failures are not silently replaced by another model. These artifacts alone do not establish officer time savings, safety improvements, or physical-box reliability.

The HTTP workflow harness is separate:

```sh
.venv/bin/python -m evals.workflow_contract --origin http://127.0.0.1:PORT \
  --sqlite /absolute/path/to/isolated-d1.sqlite --isolated \
  --device-token-file /private/path/device-token \
  --internal-token-file /private/path/internal-token \
  --device-id CONFIGURED_DEVICE_ID \
  --model-counter-file /private/path/local-service-invocation-counter \
  --output research/workflow-contract.json
```

Only use an empty disposable local Site database, with real Modal workers disabled for that test instance. The harness refuses remote origins and does not delete user data. It seeds ready drafts to isolate infrastructure from AI behavior and calls the actual Site routes. B01 tests cancellation/no write, B02 exact retry and expiry, B03 changed-body conflict, B04 role separation/no writes, and B08 bounded worker failure/claim recovery/stale leases. The optional model-call counter must be maintained by the isolated service test double; if absent, B04's no-model-invocation assertion is explicitly **unmeasured and fails**, rather than being silently credited. B08 simulates failure callbacks and lease expiry; it is not evidence of a real Modal outage rehearsal. The physical box, public deployment, and real scheduled reconciliation still require their own checks.

No scored model comparison or live workflow result is bundled with the harness. Populate claims only after executing it.
