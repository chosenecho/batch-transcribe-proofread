# Editorial recovery evaluation

`scripts/evaluate_editorial.py` replays explicitly selected saved calls with no
network access. It opens the source SQLite database in `mode=ro`, checks the
frozen snapshot digest and exact request chunk/prompt, and runs the existing
`parse_response` and `validate_revision`. No reference, paragraph or response is
modified. A new `deepseek-v2` feedback request is only previewed by its hash.
Historical input identity, request policy and evidence remain intact.
Only original physical provider calls are accepted; replay/local-precheck
records are rejected. Persisted request fingerprints are verified. Historical
calls lacking fingerprints are marked `unavailable_legacy`; their full original
request controls are checked against the frozen config, without inventing a
historical fingerprint.

```bash
PYTHONPATH=src python scripts/evaluate_editorial.py \
  --database /path/to/private/archive.db \
  --call-id SAVED_CALL_ID > /path/to/report.json
```

Select one to five distinct calls. The report includes provenance, request and
response hashes, strict validation diagnostics and actual historical usage;
it excludes transcript prose and full requests/responses. The source database
is a private prerequisite and is not committed to this repository.

## Historical sample replay on 2026-10-12

Three exact saved production failures from the local
`local-upgrade-1791603458/archive-v2-workset` copy reproduced
`cross_paragraph_reference` with the current validator. All snapshot digests
and stored request chunks/prompts passed identity checks. These calls predate
the specific 2026-10-11 incident in #350; they are not that incident's evidence.

| Saved call | Part | Editable segments | Invalid references | Prompt tokens | Completion tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| `94a8efdd979341929aec5fdfc6837280` | 1564 | 499 | 3 | 34655 | 19500 |
| `e139c9b7b2b64f8988b8130a4146f37c` | 163 | 255 | 1 | 47285 | 28565 |
| `003e71a95d6c4d13898704164ed58244` | 117 | 369 | 1 | 47133 | 32344 |

All three inputs retain `legacy-v1` request semantics. Offline replay made zero
physical provider calls; it proves reproducibility and feedback construction,
not improved regeneration success rates or semantic quality. The regression
suite separately verifies that a first successful chunk is called once when a
second chunk fails cross-paragraph validation, regenerates legally, and commits
the complete revision while retaining the failed response.

## Quality and parameter evidence

HTTP acceptance, valid JSON, strict structural acceptance and human-reviewed
semantic quality are separate outcomes. A provider accepting `reasoning_effort`,
`temperature` or `top_p` does not prove that it enforced the setting. Keep
unverified proxy behavior explicit, including after HTTP 200.

Before changing defaults, compare three to five representative parts with the
same base/reference evidence. Record proper-name errors, missed corrections,
meaning changes and unsupported additions with human-reviewed counts, alongside
validation failures, latency and actual token usage. Suggested comparisons are
context 2 versus 4, high versus max for complex terminology, and top_p .95 versus
1.0; change one setting at a time. Structural acceptance cannot supply those
semantic ratings. This replay report supports no production default changes.

## Opt-in provider comparison

The CLI defaults to offline replay. A provider comparison requires
`--allow-paid-calls`, exactly one saved call, and a dedicated private SQLite
`--ledger`. It compares disabled thinking with temperature .2, then enabled
thinking with effort low, retaining the exact historic chunk and system prompt.
These are explicitly new request configurations; they do not update the frozen
source input or production jobs. The two modes change several controls together,
so this comparison is an endpoint smoke check, not an isolated causal estimate
of temperature or effort.

```bash
PYTHONPATH=src python scripts/evaluate_editorial.py \
  --database /path/to/private/archive.db --call-id SAVED_CALL_ID \
  --allow-paid-calls --ledger /path/to/private/experiment.db \
  --endpoint https://opencode.ai/inference/go/v1 --model deepseek-v4.1-flash \
  > /path/to/sanitized-report.json
```

The existing `DeepSeekClient` obtains only the endpoint's API key environment
variable (`DEEPSEEK_API_KEY` or `OPENCODE_API_KEY`). Keys never enter request
evidence. The ledger includes full model requests and responses, including
source prose, so keep it private and outside published artifacts. Standard
output contains only the sanitized report.

Each experiment permits **four physical reservations total**, **two per
variant**, **8192 output tokens per call**, and a **180-second response timeout**.
The output cap includes reasoning tokens. Larger historic chunks may fail with
`finish_reason=length`; report that real failure rather than shrinking the
original sample or weakening validation. A strict cross-paragraph failure alone
allows the second call with the existing canonical feedback policy. Truncation,
HTTP failures and unknown sends are not automatically retried.

SQLite `BEGIN IMMEDIATE` commits the reservation before HTTP, outside the
network transaction. Failed and unfinished sends remain consumed. Restart
reuses completed evidence, never resends an uncertain reservation, and refuses
changed sample/config/budget identity. It checks the canonical manifest body,
table schema, contiguous reservation ordinals, known variants and both call
caps before sending, and refuses a corrupted ledger without resetting it.
Reusing the same ledger preserves the
cap; creating another ledger is a separately authorized experiment. Token
limits bound output volume, not a monetary price: historical input length and
current provider pricing still affect cost.

As checked on 2026-10-12, the official DeepSeek reference documents top-level
`reasoning_effort`, temperature effective only without thinking, and top_p
effective only with thinking in .95–1.0. Disabled thinking fixes effective top_p
to 1. These documented official semantics support the request adapter's shape;
they do not establish OpenCode Go enforcement.
[DeepSeek Chat Completions reference](https://api-docs.deepseek.com/api/create-chat-completion/).

The initial implementation performed no paid provider calls. Accordingly,
OpenCode Go HTTP compatibility, control enforcement and semantic quality remain
unverified. The harness records observed HTTP response acceptance separately
from strict validation and keeps `provider_parameter_enforcement=not_verified`
and `semantic_quality=not_reviewed`. Populate human-reviewed quality ratings
before using any results to change defaults.

## Authorized campaign and retrospective recovery

`--campaign --allow-paid-calls` accepts one to five distinct original source
calls in one private ledger. The immutable campaign permits **twenty physical
reservations globally**, at most **two per source/mode**, with **8192 output
tokens per call** and **180 seconds response timeout**. The same two modes are
compared in source-list order. A genuine historical cross-paragraph failure
starts with its saved diagnostic feedback; other source chunks start without
feedback. Reports identify `historical_feedback_seeded` explicitly. This is a
new capped request configuration with the exact original source chunk and
system prompt, never a reclassification of the historical request identity.

The manifest freezes source snapshot IDs, full request/response/chunk hashes,
diagnostics and every new config. Source chunk caches are checked against the
hashed snapshot's actual source text, overlapping captions, permissions and
context. Two concurrent processes share SQLite reservations; a second process
does not resend an in-flight or unknown reservation. Completed responses have
persisted checksums, and every prior variant's request chain is checked before
any further send. A changed ledger, source order, budget, config, schema,
request or response fails closed. Never create a second ledger to evade the
authorized batch limit.

After the campaign, `write_review_packet(samples, ledger, private_path)` writes
source/output pairs for semantic review. Its full prose remains private.
Report each reviewer's identity and kind (`human` or `agent`) and concrete
findings in proper-name miscorrections, missed corrections, meaning changes
and unsupported additions. An agent assessment does not claim human review.
`reasoning_content_observed` and actual reasoning-token counters supply mode
observations only; they do not prove every provider sampling control was
enforced. Defaults remain evidence-gated.

For a free retrospective comparison, use `--historical-recovery` with up to
fifty exact historical failed `--call-id` values. The source is opened read-only.
For each failure it observes at most two later physical calls for the same
input/chunk with the same original request controls. Cached response replays,
local prechecks and changed settings are excluded. Unknown sends consume the
observation window; missing follow-up is censored. Actual usage is summed with
missing counters tracked, without estimating unknown usage as zero.

The [2026-10-12 retrospective report](../evidence/editorial-natural-recovery-2026-10-12.json)
records all nineteen reproducible cross-paragraph failures found in the private
historical copy, whose database SHA-256 is
`0c093c2b65c555d695d9ba958a989af1c4008ed56c75bf2819b9cf91265d4616`.
Sixteen have a later physical call under the same controls; fifteen pass full
strict validation, an observed recovery rate of **15/16 (93.75%)**. Three have
no observed follow-up, and one observed follow-up remains rejected. The sixteen
additional calls consumed **856673 prompt**, **497643 completion** and **351284
reasoning tokens**, all from stored provider counters. They are natural retries
without diagnostic feedback, from an earlier cohort than #350's named incident.
This proves that unchanged requests can recover and gives actual additional
usage; it does not estimate a causal improvement from the new feedback policy.
The report made **zero new provider calls** and changed no source rows.

## Authorized OpenCode campaign result, 2026-10-12

The [sanitized campaign evidence](../evidence/editorial-provider-campaign-2026-10-12.json)
records ten completed physical calls in one ledger, with no unknown sends or
transport retries. All ten HTTP responses were accepted by OpenCode Go with
model `deepseek-v4.1-flash`. The five exact original chunks have 222, 39, 40, 63
and 68 editable segments. Each was sent once in each mode; requests and
responses remain in the private ledger. No production jobs or defaults changed.

| Mode | HTTP accepted | Full strict validation | Actual prompt tokens | Actual completion tokens | Reasoning tokens | Total observed latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| disabled + temperature .2 | 5/5 | **4/5** | 77082 | 12282 | 0 | 87.005 s |
| enabled + effort low | 5/5 | **0/5** | 77207 | 40960 | 40960 | 333.077 s |

All disabled responses ended with `stop`. The 39-segment sample's response
included an extra top-level `issues` field and failed the unchanged strict
schema (`invalid_fields`, expected two fields, received three). It was kept as
failed evidence; no field was removed to turn it into an accepted result.
The genuine 222-segment historical cross-paragraph failure passed strict
validation after one new disabled-mode request seeded with its real diagnostic,
consuming 48812 prompt and 6833 completion tokens. This is one recovery under
new controls; it is not an estimate of a causal improvement from feedback.

All five enabled-low responses ended with `length`, spending the entire 8192
completion-token cap on reasoning and returning no final JSON. They failed
strict parsing and cannot be assessed for final-text semantic quality. The
runner stopped after ten calls: truncation and the unrelated top-level schema
error do not qualify for cross-paragraph regeneration. The unused allowance
was not converted into blind retries.

The response evidence supports an observed distinction between thinking modes:
disabled reports zero reasoning tokens and final JSON, while enabled returns
reasoning content and nonzero reasoning tokens. The effective strength of
`reasoning_effort=low`, temperature .2 and top_p remain **not verified**; HTTP
200 and these mode observations do not establish the implementation of each
sampling control. Cache-hit and cache-miss counters are absent in all ten
responses and are recorded as missing rather than claimed as zero.

An agent reviewed all 64 paragraphs from the five disabled responses against
their declared source segments and saved overlapping captions on 2026-10-13.
Four accepted candidates contain 55 paragraphs; the rejected response's nine
paragraphs were examined for diagnostics only. The private review retains
specific source/reference/output evidence and hashes. It is explicitly
**agent review, without audio review or human approval**. The rubric counts
observed findings, at most one per category per paragraph; they are conservative
qualitative counts, not error rates against an authoritative ground truth.

| Source call | Paragraphs reviewed | Strict accepted | Name miscorrections observed | Missed corrections / unresolved editing defects | Meaning changes | Unsupported additions observed |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| `acf445158b9d42d89920408532bd17b0` | 34 | yes | 0 | 12 | 1 | 0 |
| `6e3500770656417295843fba608f2cf3` | 9 | no | 0 | 2 | 0 | 0 |
| `69e419ceb18844d799f14b394fa921df` | 7 | yes | 0 | 2 | 0 | 0 |
| `6fae80961fa04ebc89b8305d316cb76b` | 7 | yes | 0 | 4 | 0 | 0 |
| `9a655130000e4511879e1df9fe60267a` | 7 | yes | 0 | 2 | 0 | 0 |

The review found repeated unedited fragments, unresolved grammar and an
observed change in the object of a statement in the long historical sample.
Zero findings in a category means none observed in this limited review, not
proof of factual correctness. Structural acceptance therefore does not support
automatic publication or a global switch to disabled thinking.

For a small output-budget compatibility probe, disabled thinking is capable of
producing reviewable JSON on these samples, with strict failures still possible.
The tested enabled-low setting is unsuitable for an 8192-token output cap on
these samples. Further enabled-mode quality comparisons would require a
separately authorized budget and adequate room for both reasoning and final
output. Context 2/4, high/max and top_p .95/1 causal comparisons were not run.
Keep production defaults and frozen tasks unchanged; choose future experiments
by observed failure type rather than declaring a universal best setting.
