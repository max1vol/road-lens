# Development integration findings

Unscored Modal smoke calls initially failed the 2,500 aggregate output-token limit (7,547 tokens observed). LOW thinking, a 4,096 response cap and a 12,000 aggregate cap were then applied equally to the production agents and all evaluation arms. The six-request, eight-tool and 90-second limits remain.

The subsequent visible tool trace identified the underlying retry loop: Gemini returned longitude 0.139839226648259 and latitude 52.2074203872718, while the source values were 0.13983922664825893 and 52.20742038727183. Exact float equality rejected this correct junction. The validator now tolerates at most 1e-12 degrees of JSON number rounding, requires every other field (including original grid coordinates, place ID and provenance) to match exactly, and emits the canonical registered source object. Unknown points and meaningful changes remain rejected. The same rule applies to local-evidence tool inputs and all three evaluation arms.

The independent loose-paving-slab development fixture also exposed an overly narrow positive-concern keyword list. That positive keyword gate was removed before scored evaluation; exact resident quotes, turn IDs, source provenance, contact and instruction checks, negation and correction handling remain.

The working voice box retains its existing extended-thinking Gemini Live setting. These changes concern the additional backend agents only.

References checked during implementation: https://ai.google.dev/gemini-api/docs/thinking and https://ai.pydantic.dev/models/google/.
