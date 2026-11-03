# 工作流失败、状态口径与恢复

`workflow status`、`workflow explain`、`workflow retry` 和周期 `retry-maintenance` 使用同一套错误政策与重试决定。
它们读取已有 jobs、attempts 和安全诊断，不修改历史错误码、冻结输入或模型调用证据。
失败分类契约为 `workflow-failure-policy/v1`，状态投影为 `workflow-failure-status/v1`，
库外控制证据读取契约为 `workflow-retry-control/v1`。这三项不安装新数据库表。
维护 allowlist 只能收窄共享政策；数据库 attempts 为累计重试消耗下限，冻结物理预算、
holds、退避与已活动/成功替代任务继续限制重排。状态只读取标量预算事实，保留模型正文。

## 状态与统计口径

```bash
bili-asr workflow status --archive-root /srv/bili-archive --failed --json \
  --control-state /srv/production-ops/state.json --limit 100 --offset 0
bili-asr workflow status --archive-root /srv/bili-archive --json \
  --since 1791734400 --until 1791820800
bili-asr workflow explain --archive-root /srv/bili-archive --job-id <job-id> \
  --control-state /srv/production-ops/state.json
```

时间参数是 UTC Unix 秒；失败窗口按 attempt 的 `finished_at` 使用 `[since,until)`。
即使一次尝试在窗口之前开始，只要在窗口内结束为失败，就计入窗口。
省略 `until` 使用报告时刻的当前秒加一；省略 `since` 查询自 epoch 起的窗口。
窗口只限制失败窗口计数，不把当前 job 状态或完整历史计数改成窗口状态。

| 输出 | 定义 |
| --- | --- |
| `current_failed_jobs` | 快照时状态为 failed 的全部 jobs；包括被保留的旧失败任务 |
| `historical_failed_attempts` | 所有 outcome 为 failed 的 attempts；可能属于已成功、排队或运行的 jobs |
| `failures_since` | 指定完成时间窗口内的失败 attempts |
| `active_retryable_failures` | 当前失败且自动政策、控制状态、预算、退避和替代关系均允许重试的 jobs |
| `retry_holds` | 显式读取控制状态中的 hold 条目数量，不等同于当前失败 jobs |
| `failed_jobs_with_active_replacements` | 已有 queued/running/succeeded 替代任务的旧失败 jobs |

这些指标描述不同维度，不能相加为总失败数。一次 job 失败三次后成功，当前失败为零，
历史失败尝试仍为三。显式 repair 成功后，旧 failed job 仍保留，并通过 `replacements`
显示新任务状态。失败组按 job kind 和原 error code 聚合，分别显示上述计数、最后发生时间、
政策和各类重试决定；一组错误不保证具有相同的 hold、预算或重试资格。

`--failed` 输出当前失败任务的安全诊断；`--jobs` 输出身份；`--details` 输出任务解释。
任务页默认 100 项，`--limit` 范围为 1–1000，`--offset` 用于分页；汇总不受任务页限制。
文本与 JSON 从同一报告对象产生。状态、解释和 retry dry-run 使用只读归档访问，
不探测外部存储、不读取模型调用正文、不运行 ASR 或付费模型。

## 库外控制证据

现有生产控制器在数据库外保存 `retry_holds` 和 `retry_ledger`。
`--control-state` 显式读取一个受大小限制的 JSON 文件，既有运维状态的其他字段可以保留。
hold 的任意理由文本及凭据不进入状态输出；只投影 job 身份、计数和整个文件的 SHA-256。

```json
{
  "retry_holds": {"held-job-id": {"reason": "operator-controlled"}},
  "retry_ledger": {
    "another-job-id": {"retries": 1, "last_retry_at": 1791734400}
  }
}
```

没有提供控制文件时，状态显示 `not_provided`，hold 数量和完整自动重试资格为 null/unknown。
不完整的控制文件也不能证明缺失的控制映射为空。文件不可读、重复键、非法计数或超限数据
拒绝读取。只有实际没有库外重试控制的部署才使用 `--no-control-state`，明确声明控制为空；
不能用该参数代替已有生产 holds。命令不会自动猜测服务器路径或改写控制文件。

## 错误政策

| 类型或代码 | 分类与默认处理 | 恢复前提 |
| --- | --- | --- |
| `deepseek_transport_failed`、已登记网络错误；供应商 HTTP 408/429/5xx | transient；有限自动退避重试 | 已知控制状态、预算和退避均允许 |
| 供应商 HTTP 402 | billing；阻止自动重试 | 恢复余额或方案，检查后精确手动恢复 |
| 供应商 HTTP 401/403、缺少 API key | configuration；阻止自动重试 | 恢复权限或配置 |
| `editorial_source_repetition`、不可用字幕正文或时间轴 | source_quality；修复来源 | 原文与音频复核，纠正转录或来源条件 |
| `RevisionValidationError`、无效/空/截断模型输出 | revision_validation；人工检查或显式修复 | 检查诊断，必要时改变提示词、分块或配置 |
| `publication_contract_failed` | publication_contract；修复数据后恢复 | 检查来源、音频和时间轴，不排序或夹紧原始时间戳 |
| 已登记产物缺失/身份错误 | artifact_unavailable；停止重试 | 恢复准确对象，不重新推理制造替代身份 |
| `lease_expired` | lease_recovery；检查租约恢复 | claim 已负责过期租约回收，不另建重复重试循环 |
| 旧 `DeepSeekError`、泛化异常、未知码 | unknown；人工检查 | 不从异常文本猜测 HTTP 状态或自动重试 |

错误码保留原拼写；新供应商错误在产生边界保存细分码和安全诊断。旧泛化记录仍可读取，
缺失信息保持未知。自动政策默认最多三次 attempts（初次加两次重试），根据已执行次数
使用有限退避；数据库 attempt_count 是消耗预算的下限，外部 ledger 只能收紧预算。
手动恢复也检查控制、替代任务、预算和退避；精确人工 override 可以在记录理由后覆盖
政策类别；预算、退避、hold 和已有活动/成功替代任务仍不能绕过。

## 检查后重排

```bash
# 控制器调用统一政策；先检查决定
bili-asr workflow retry --archive-root /srv/bili-archive --automatic \
  --control-state /srv/production-ops/state.json --dry-run --json
# 实际执行同一决策用例
bili-asr workflow retry --archive-root /srv/bili-archive --automatic \
  --control-state /srv/production-ops/state.json --json
# 人工检查后，精确恢复需要修复/配置前提的一个 job
bili-asr workflow retry --archive-root /srv/bili-archive --job-id <job-id> \
  --control-state /srv/production-ops/state.json --manual-override \
  --override-reason "external condition repaired and evidence reviewed" --dry-run --json
```

去掉 dry-run 才重排。普通模式为手动，`--automatic` 明确采用自动准入。
manual override 要求恰好一个 `--job-id`、有界非空理由，不能与 part/kind 批量选择或
automatic 混用。报告保存理由摘要，任意理由文本不进入公共诊断。
预算耗尽时不会用人工 override 延长同一失败链，应检查原因并采用受支持的新输入修复。
命令逐项说明 eligible、policy_denied、control_state_unknown、retry_hold、backoff、
budget_exhausted、replacement_exists 等决定，不把跳过等同于成功重排。

实际写入在 JobCommitGuard 的短事务中复验 job 状态、错误、attempt 身份和控制文件摘要；
变化会拒绝或回滚。SQLite 与库外文件没有跨介质原子事务，控制文件所有者必须串行化
hold/ledger 更新与重试调用，摘要复验不代替这项交接。worker 只记录失败；控制器负责
调用重试用例，供应商客户端不隐式重试，也不新增进程内自动重试循环。

旧库级 `WorkflowRepository.requeue_failed()` 仍保留手动兼容能力，缺少库外控制语义；
新的控制器应改用 `guarded_retry_failed()`。其 automatic 参数拒绝绕过统一决策，
finish/fail 的内联 retry_at 路径同样拒绝未经检查的自动重排。部署旧控制脚本时必须
显式迁移调用点，不能仅升级 wheel 就声称脚本已经采用新政策。

## 编辑失败诊断与修复

ASR 推理完成与下游可用性按[ASR 准入与显式修复](asr-admission-and-repair.md)
分别保存。倒退、越界、覆盖异常和历史缺证据不进入付费校对；原始推理和 hold
保留。使用 `workflow asr-quality` 只读查询，`workflow repair-asr` 明确创建修复链。

安全诊断保存 stage、reason_code、input/call/chunk 身份、paragraph/issue/segment 位置、
计数、阈值或非法引用摘要。provider_request 表示供应商请求，response_parse 表示响应
解析，revision_validation 表示结构校验；source_quality 在调用模型之前检查来源。
不复制来源段落、响应正文、请求 URL、密钥或自由异常文本。历史缺失或非法诊断明确
显示 missing/unavailable/redacted；原始模型响应仍保存在 model-call 证据中供受控核对。

```bash
bili-asr workflow repair-proofread --archive-root /srv/bili-archive \
  --job-id <failed-proofread-job-id> --max-chunk-segments <changed-positive-limit>
bili-asr workflow repair-proofread --archive-root /srv/bili-archive \
  --job-id <failed-proofread-job-id> --base-transcript-id <corrected-transcript-id>
```

repair 冻结新的输入并建立新的 proofread/render 依赖链，旧 job、attempt、冻结输入、
模型响应、已完成块、旧 render 和 hold 保留。相同输入的 repair 会拒绝；普通 retry
继续使用原冻结输入。源重复检查在原始 ASR segment 上进行，缩小块或换提示词不能
修复异常源转录。切换供应商/模型属于冻结配置变化，应走新输入修复。
普通重试可以复用已经验证的块，但未持久化响应的在途供应商调用可能重复计费。

更多编辑参数与审核边界见 [AI 校对指南](../guides/ai-proofreading.md)，
实现边界见 [当前架构](../architecture.md) 和 [失败恢复时序](../30-editorial-repair.md)。
