The first deployed development smoke produced a real Gemini response, then failed Pydantic AI's aggregate output budget: `UsageLimitExceeded`, configured 2,500 versus 7,547 actual output tokens. Google's Gemini token usage includes thinking. The key/model/SDK were therefore functioning; this was not an authentication or model availability error.

For backend IntakeAgent and EvidenceAgent, use shared `AGENT_MODEL_SETTINGS` with `google_thinking_config={'thinking_level':'LOW','include_thoughts':False}`, `max_tokens=4096`, and shared `AGENT_OUTPUT_TOKEN_LIMIT=12000`. Six requests, eight tool calls and the 90-second deadline remain unchanged. Apply identical settings to all evaluation arms. The existing Gemini Live extended-thinking configuration is untouched.

Also remove the positive `CONCERN` keyword gate from output validation: a valid independently authored observation about a loose paving slab was excluded by its vocabulary. Exact resident quotes, source turn IDs, resolved location/provenance, contact removal, instruction and explicit negation checks remain. This is a general domain-boundary fix, not a held-out fixture change.

Official references checked: https://ai.google.dev/gemini-api/docs/thinking (3.8 Flash supports LOW/MEDIUM/HIGH, defaults MEDIUM); https://ai.pydantic.dev/models/google/ (GoogleModelSettings native thinking config).
