# ADR-007: Retry / backoff policy for transient LLM failures

**Status:** Accepted (2026-05-15)

## Context

ADR-006 §1's error-strategy table chose **zero infrastructure retries** for Slice 1, with a follow-up clause: *"queued for a follow-up ADR if Phase 2 calibration shows transient errors are common enough to warrant it."* Phase 2 Slice 8 calibration (five workflows, no API 5xx, no timeouts) did not surface real transient errors. That is calibration evidence about a single session's luck, not a general-rate finding — a real production deployment will see them.

`llm.complete()` is the single entry point for every LLM call in the project (CLAUDE.md rule 9). Today a single transient failure orphans the whole workflow: the calling agent raises out, `run_step` marks the step FAILED, and the orchestrator routes to `halt` with `workflow.status = FAILED`. Recovery is possible only by re-running from CLI; LangGraph's checkpointer preserves state up to the failing step. That is acceptable when transient errors are rare, but at production volumes the 429/5xx/network rate dominates the perceived reliability — one transient hit per N workflows is unacceptable if N is small.

This ADR commits to a bounded infrastructure-retry policy inside `llm.complete()`, layered cleanly underneath ADR-006 §1's fabrication-retry edge. The two retry mechanisms target different failure shapes and compose without interaction.

### What the habitat already provides

- `llm.complete()` — single LLM entry point with structured logging and JSONL telemetry. The retry helper lives inside this function; agent code is unaffected.
- `LLMResult` — return contract; the retry helper does not change this shape.
- Anthropic SDK exception hierarchy — `RateLimitError`, `InternalServerError`, `APIConnectionError`, `APITimeoutError`, and `APIStatusError` (status-code-bearing). These are the classes we discriminate on.
- ADR-006 §1's fabrication-retry edge in the orchestrator — independent of this ADR; this ADR's retry happens strictly underneath that one.

---

## Decision

`llm.complete()` wraps its single `client.messages.create(**kwargs)` call in a bounded retry helper. The helper is a small pure function in `llm.py` (no new dependency); it is exercised through deterministic tests via dependency-injected `sleeper` and `rng` hooks.

### 1. Which errors retry, which do not

| Class | Retry? | Rationale |
|---|---|---|
| `RateLimitError` (HTTP 429) | YES | Backoff is exactly the right remediation; honour the `Retry-After` header when present. |
| `InternalServerError` (HTTP 500) | YES | Anthropic-side transient. |
| `APIStatusError` with status in `{500, 502, 503, 504}` | YES | Bad-gateway / service-unavailable / gateway-timeout — all transient. |
| `APIConnectionError` | YES | Network blip; reconnect on retry. |
| `APITimeoutError` | YES | Slow response; retry with a fresh socket. |
| `BadRequestError` (HTTP 400) | NO | Code bug — body or schema is wrong. Retrying re-pays for the same broken call. |
| `AuthenticationError` (HTTP 401) | NO | Credential is wrong; no amount of waiting fixes it. |
| `PermissionDeniedError` (HTTP 403) | NO | Authorisation; same logic as 401. |
| `NotFoundError` (HTTP 404) | NO | Model/endpoint typo; structural. |
| `UnprocessableEntityError` (HTTP 422) | NO | Validation; structural. |
| Any other `APIStatusError` (e.g. 4xx not listed) | NO | Default-deny: if it isn't explicitly retryable, surface it. |
| Anything not an `APIStatusError` subclass | NO | Out of scope; non-API exceptions (e.g. our own ValueError) are bugs, not transient. |

### 2. How many retries

**Three attempts total** (1 initial + 2 retries). Chosen because:
- One retry is too few — a single transient blip beats a workflow.
- Three retries is enough — a sequence of three independent transient failures on the same call is rare enough that the underlying system is genuinely degraded; halting then is the right answer.
- Total wall-time cost on the maximum-retry path is bounded by §3's backoff: roughly 1s + 2s = ~3s of sleep plus the per-attempt request times. That stays under the 30-60s typical request budget on a healthy LLM call.

### 3. Backoff strategy

**Exponential backoff with jitter:** `sleep_seconds = base * (factor ** (attempt - 1)) + jitter` where `base = 1.0`, `factor = 2.0`, `jitter` is uniform `[0, 0.5)`. After attempt 1 (the initial call) fails, the helper sleeps ~1s (+ jitter) before attempt 2; after attempt 2 fails, the helper sleeps ~2s (+ jitter) before attempt 3.

**Retry-After header honoured on 429s.** When Anthropic returns a 429 with a `Retry-After` header, the helper sleeps for that many seconds instead of the computed backoff. This respects the server's own load signal — exponential backoff that runs faster than the server's stated cooldown is wasted retries against a known-rate-limited account.

Jitter is `random.uniform(0, 0.5)` — a thundering-herd guard. With one process today this is mostly cosmetic; with multiple processes (Phase 3+) it matters.

### 4. Composition with the fabrication-retry edge (ADR-006 §1)

The two retry mechanisms are **independent** and compose without interaction:

- **Infrastructure retry (this ADR)** sits *inside* `llm.complete()`. It fires when the API call itself didn't complete (transport-layer failure). On success — even on attempt 3 — it returns one `LLMResult` to the caller. The caller cannot tell whether retries happened (except via the new `attempts` field in JSONL telemetry).
- **Fabrication retry (ADR-006 §1)** sits *above* `llm.complete()`, in the orchestrator's critic-routing edge. It fires when the draft was successfully generated but failed the substring grounding check. It re-invokes the drafter (which makes a fresh `llm.complete()` call, which has its own retry budget).

Concretely: an infrastructure retry that succeeds yields a response that may or may not pass the substring check; if it doesn't, the fabrication retry edge fires normally and the drafter is invoked again (with its own independent infrastructure-retry budget). The two budgets do not pool. This is intentional — they are remedying different failure modes and conflating them would cap recovery prematurely.

### 5. Cost tracking

Each retry attempt that hits the API potentially costs money. Anthropic's billing posture:
- **429 (RateLimitError)** — request rejected before processing; not billed. Our cost accounting reflects this by recording `cost_usd = 0` for failed-and-retried attempts.
- **5xx after token generation** — Anthropic may bill the partially-generated tokens (the `usage` object is present on some 5xx responses). Our cost accounting **does not** attempt to capture this; the failed attempt's `usage` is unavailable to us (the call raised before the response object was returned).
- **Eventual success** — `cost_usd` is computed from the success's `usage` and is the only cost we record per `complete()` invocation. This may under-report by the cost of partially-generated 5xx tokens; the magnitude of that under-reporting is bounded by the retry budget and is far below the cost of an outright drafter retry.

The JSONL telemetry record gains a new top-level integer key `attempts` (default 1 — backward-compatible: pre-this-ADR records did not have the key, which is indistinguishable from "one attempt"). The `cost_usd` field remains the eventual-success cost. If post-Phase-3 calibration shows under-reporting is material, a follow-up addendum can decide whether to capture failed-attempt `usage` via a separate SDK code path.

### 6. Streaming

The project does not currently use streaming (`client.messages.stream` or `stream=True`). This ADR is therefore scoped to non-streaming `messages.create` calls. If streaming is added later, retry semantics for partial streams need a separate decision (mid-stream retry would re-issue from the beginning, paying for the consumed-so-far tokens — likely a different retry budget).

### 7. Implementation shape

A small helper `_call_with_retry` in `llm.py` takes the call as a no-arg `Callable`, plus injectable `sleeper: Callable[[float], None]` and `rng: random.Random` parameters. The helper returns `(result, attempts_made)`. `complete()` calls this helper around `client.messages.create(**kwargs)`, threads `attempts_made` into the JSONL record, and is otherwise unchanged.

The injectables are module-level defaults (`_SLEEPER = time.sleep`, `_RNG = random.Random()`); tests patch them via `patch.object` to make retry behaviour deterministic without real wall-clock waits.

---

## Alternatives Considered

| Option | Pros | Cons | Rejected because |
|---|---|---|---|
| **Bounded hand-rolled retry (chosen)** | No new dependency; ~30 lines; tests are direct. | Slightly more code than a library. | Library overhead unjustified for a fixed policy. |
| **Tenacity** | Battle-tested; rich decorator API. | New dependency for a fixed policy; typing on decorators is fiddly under strict mypy. | The decorator surface is bigger than the problem; one helper function is enough. |
| **httpx-level retry transport** | Sits below the SDK; uniform across SDK versions. | Anthropic SDK uses httpx internally but constructing a custom transport requires SDK-private knowledge; brittle. | Wrong layer — error classification is an SDK-level concern (RateLimitError, etc.), not a transport-level one. |
| **Unbounded retry with circuit breaker** | Survives long outages without operator intervention. | Adds complexity (breaker state, half-open probes); hides the failure rate from the calibration story. | Premature; bounded retry surfaces persistent failure as a workflow FAILED, which is the honest signal. |
| **Five attempts instead of three** | Higher per-call success rate. | Longer worst-case wall time; diminishing returns past 3. | Three covers the vast majority of independent-transient sequences; five is over-fitting to imagined failure modes. |
| **No Retry-After header honouring** | Simpler helper. | Wasted retries against a rate-limited account. | The server's load signal is free information; ignore it and you re-trigger the same 429. |

---

## Consequences

**What becomes easier:**

- Single transient failure no longer orphans a workflow; one rate-limit blip costs ~1s of sleep instead of a full re-run.
- Operators see fewer FAILED workflows in the audit table; the failure rate that remains is the genuine persistent-failure rate, not a sample of transient noise.
- The JSONL telemetry `attempts` field gives calibration evidence about real-world transient rates — Phase 3+ can re-tune the retry budget from data.

**What becomes harder:**

- Wall-clock latency on a degraded API can grow by up to ~3s per call (the worst-case retry budget). Acceptable for non-interactive workflows; would matter for a real-time chat surface (not the current product).
- `llm.complete()` is no longer a thin wrapper; tests assume the retry happens. New tests must consider retry semantics.

**What is deferred:**

- **Streaming retry semantics.** Add when streaming is added.
- **Partial-5xx cost capture.** Add if calibration shows material under-reporting.
- **Cross-workflow rate-limit coordination.** Today every workflow retries independently; under multi-process load this could deepen a rate-limit hole. Phase 4+ if multi-process orchestration is wired.
- **Per-tier retry budget.** A single budget applies to Haiku, Sonnet, Opus today. If calibration shows tier-specific failure modes, split.

**What would invalidate this decision:**

- Calibration shows three retries are insufficient for normal load — the budget is genuinely too tight.
- A future Anthropic SDK version exposes a built-in retry mechanism with comparable semantics — adopt it and supersede this ADR.
- A workload appears that needs interactive latency below ~5s on a degraded API — re-evaluate the retry budget against that constraint.
