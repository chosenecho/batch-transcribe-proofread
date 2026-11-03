# Editorial production recovery: #456, #457, existing #350

## Scope and architecture

The server is shut down. This change is validated with local SQLite fixtures and fake provider clients only; no SSH, provider requests, production requeue, or artifact deletion is performed.

* #457: classify the provider's finite termination vocabulary at the response boundary. Keep `length`, `content_filter`, `stop` with empty body, unsupported tools, missing and unknown termination distinct. Public diagnostics contain only enumerated reasons, counts and bounded usage. Preserve stored raw request/response and usage. A preserved filter/protocol termination blocks another physical send for the same frozen input, including legacy generic errors. The status/retry facade derives that diagnosis without rewriting historical error codes.
* #456: add an explicit application use case and CLI for reconciling downstream consumers after an exact operator-selected ASR replacement succeeds. Reuse existing frozen editorial inputs, JobSpecs and render template resolution. Do not modify original payloads, dependencies or attempts. Create new proofread/render jobs with `repair_of_job_id` provenance and exact replacement attempt/run identity. Retire only queued, never-attempted consumers, in the same writer transaction. Running, attempted, held and ambiguous chains are rejected. A stale plan, control change, or interrupted enqueue rolls back the entire operation.
* #350: retain the existing strict output validator, allowlisted bounded paragraph-reference regeneration and explicit `repair-proofread` flow. Unknown references, paragraph formatting, and missing fields remain invalid. The new three-family fixture verifies an explicit changed-input repair can succeed while preserving original failed attempts/calls. This does not mean the ten saved production outputs have become valid.

## Operator workflow after deployment

First verify the installed complete release and worker identities. Inspect both original failures and exact successful ASR outputs with measured source-quality evidence and the current retry-control snapshot.

| Part | Original proofread | Explicit successful ASR replacement |
| --- | --- | --- |
| 460 | `ca0e7f63-3118-4148-be00-09a4822910f8` | `cdae00eb-29c0-4676-8cf5-e909280a750b` |
| 675 | `47e45f98-8f79-46ad-ad87-0b20e5c2aa5f` | `67c2f590-5a16-4a0a-ab7c-a0974ddbaafa` |

Use `bili-asr workflow recover-editorial --archive-root <root> --proofread-job-id <old-proof> --replacement-asr-job-id <exact-asr> --control-state <snapshot>` for a read-only plan. The report distinguishes `producer_recovered` from `delivery_recovered`, includes old/new consumer identities, frozen input, exact ASR attempt/run, hold blockers, physical request budget and retention follow-up. A deployment without external controls must explicitly declare `--no-control-state`.

After inspecting the plan and budget, repeat the command with `--apply --expected-plan-id <plan_id>` to queue replacement consumers. This use case never composes or executes an AI worker. Repeating the same recovery is idempotent and follows the existing replacement. Legacy request policies remain legacy and report their physical-call ceiling as unknown rather than falsely claiming the new bound; review/authorize any new paid work and use existing changed-input repair when a new policy is required.

Reinspection validates the recorded replacement graph as well as its frozen payload:
the new proofread job must depend only on the selected ASR, and the new render
job only on that proofread job, with both consumers on the original part.
Missing, extra or redirected edges and duplicate matching render identities
are rejected before retiring consumers or reporting the recovery as complete.
The recovery operation does not silently repair those inconsistencies.

Only start editorial workers when the current request budget is authorized. Reinspect completed rendering and frozen source selection before considering delivery recovered or changing retention. Old ASR failures and their attempts remain evidence. This feature does not authorize audio deletion or clear retry holds.

For historical `content_filter`, stop-empty, and contract failures, inspect the stored envelope and usage offline first. A filter is not a truncation; do not keep buying identical requests or silently change model/prompt/sampling. Any approved changed request uses an explicit new frozen input.

A `stop` response with an absent `content` field, reasoning only, null content,
or whitespace content is `deepseek_empty_output`. Its safe diagnostics retain
`finish_reason=stop`, character counts and bounded usage; the complete envelope
remains in the private call record. Reasoning tokens do not prove that a usable
revision was returned. The response still fails the output gate.

## Regression evidence and production limits

Coverage includes read-only planning, CLI plan/apply, frozen producer selection at execution, idempotency, holds, running/attempted refusal, stale plans, transactional interruption, legacy budget reporting, finite diagnostics, historical generic filter replay, scalar status projection and prevention of a second physical send. Deliberately disabling the provider retry guard makes the regression fail with two fake requests instead of one; the guard is restored afterward.

Production verification remains pending: the two actual chains, the ten #350 samples, actual provider behavior, GPU runtime, and backup eligibility must be inspected after the server is explicitly restarted. Offline tests do not establish successful paid production recovery or release acceptance.
