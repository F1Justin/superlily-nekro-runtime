# Argon prompt caching

OpenRouter `google/gemini-4-argon` (including dated and variant IDs) uses the
implicit-cache request path. Other models keep the existing explicit Gemini
breakpoints. Per-chat hashed `session_id` routing and provider usage accounting
remain enabled.

Argon historical records are serialized as quoted JSON arrays before the changing
clock, plugin runtime context, memory, reply focus, and image/audio attachments.
Message IDs are included consistently so reply-focus changes do not rewrite old
ordinary records. Quotes, newlines, and markup in participant text remain data;
angle brackets are escaped to prevent impersonating the outer history block.
Each record remains a separate user message. A fixed assistant framing marker,
explicitly described by the English and Chinese core policy as runtime metadata
and not a real reply/action, prevents adjacent user turns from collapsing into
one changing history message. The dynamic tail remains a separate final user
message. Appending a record therefore preserves earlier message boundaries.

The runtime `one_time_code` remains random for each run and stays in the dynamic
tail. The existing sandbox output-leak check remains unchanged. Native media and
explicit reply focus keep their existing nonce-bearing rendering and budgets.
The change does not enlarge configured history limits or add token padding.

A 4096-token total input is not a guarantee of a cache hit. The repeated prefix
must be sufficiently long; an edited or rolling history window can invalidate
the prefix. Short conversations continue normally with no cache discount.

Validation: `poe lint` and targeted cache, runtime, web-search, voice, and egress
regressions. Real-provider replay must inspect returned `cached_tokens`, not the
provider metadata capability flag. Replays never execute generated Python or
send messages to a chat.

## 2026-10-14 validation

- 81 targeted regression tests passed, plus `poe lint`.
- Tests explicitly fixed the model ID to `google/gemini-4-argon`; the production
  model group had independently switched back to Gemini 3 Flash during the
  investigation. Earlier successful Flash probes are not Argon evidence.
- With synthetic Chinese records through the SDK, a fixed assistant framing
  marker yielded 5,345 cached tokens out of 7,395 input tokens. Appending one
  record retained 5,346 cached tokens out of 7,470 input tokens. Every generated
  response parsed as Python; none was executed.
- The final image passed a stricter six-request acceptance test: 5,377 cached
  tokens before append and 5,378 / 5,453 after append. Some requests hit only
  673 tokens of the static prefix; this is observed implicit-cache variability,
  not a promise of a fixed cache rate.
- Adjacent user messages and an in-message dynamic suffix did not reliably
  cache in the same tests. Implicit caching can require warm-up; do not promise
  a hit on every request or infer a higher fixed threshold from isolated misses.
- Separate fixed-model, synthetic HTTP tests confirmed cache hits through both
  Google Vertex and Google AI Studio. `cache_control` did not report explicit
  cache writes for Argon; legacy explicit handling remains scoped to older models.
- Real-history external replay was rejected by automatic approval review and
  was not performed. All real-provider validation used synthetic prompt data.

Deployment uses `docker/argon-cache.Dockerfile`, an overlay on the existing egress
image, preserving dependency, sandbox, voice, and proxy settings. Roll back by
restoring the previous Compose image `2.3.3-superlily.13-egress.1` and Dockerfile
path, then recreating only `nekro_agent` with `--no-deps --no-build`.
