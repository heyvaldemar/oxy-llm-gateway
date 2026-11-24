# vLLM (Local)

Use vLLM as a local provider via its OpenAI-compatible endpoint.

- Endpoint: default `http://localhost:8000` (override with `VLLM_BASE_URL`).
- API: OpenAI-compatible `/v1/chat/completions`; Oxy sends OpenAI-format requests.
- Models: use `model="vllm/<name>"` (e.g., `vllm/mistral-7b`).
- No provider key is needed. A server started with its own key (`--api-key`) gets it as `VLLM_API_KEY`, or on the organization's provider page; Oxy sends it as a bearer token.
- Price: a local model bills nothing per token unless you set `LOCAL_PROMPT_PRICE_PER_MILLION` and `LOCAL_COMPLETION_PRICE_PER_MILLION` (USD per million tokens, for example your GPU's amortized cost). Set them and a project's budget caps the model like a hosted one.
- Guardrails: DLP/privacy/budget checks run in the same path as cloud providers; the `local` proof in CI measures it against a recording fake.
- Limitations: performance/latency depend on your deployment; ensure the model is served in vLLM.
