# AI 校对使用说明

阅读稿通过兼容 Chat Completions 的接口处理固定数据库转录。模型标识、请求策略和预算以源码及冻结配置为准；本次未重新调用付费服务核验供应商规格。
AI 返回带来源 ID 的完整阅读段落，程序校验后保存独立修订，生成按冻结输入语言整理的 Markdown 阅读稿和校对记录。
业务进度保存在 SQLite；工作器重启后读取固定输入与已有块结果继续执行。

## 运行前提

- Python 3.12+ 与项目基础依赖。仅运行校对和文档渲染不需要 GPU、ASR 模型或 ffmpeg。
- 归档数据库已存储基础转录。默认使用所选视频部分最新的本地 ASR 版本。
- 固定输入/content 与 AI/publication 模板均支持已登记的 v1/v2；历史字节按记录版本读取，新 universal 稿件使用 v2。未知 schema 在写入前拒绝，详见 [契约登记表](../contracts/registry.json)。
- 新建数据库使用包含 `proofread` / `render_document` 的任务类型，以及支持 `cancelled` attempt outcome 和终态约束的工作流表结构。
- 真实校对需要环境变量 `DEEPSEEK_API_KEY`。密钥仅进入 HTTP Authorization，不写入模型请求快照、结果或文档。
- 使用 OpenCode Go 时设置 `OPENCODE_API_KEY`，并将 endpoint 设为 `https://opencode.ai/inference/go/v1`。旧的 `https://opencode.ai/zen/go/v1` 仍兼容已有冻结任务。客户端会发送 `User-Agent` 和每个进程会话稳定的 `x-opencode-session`，密钥仍只进入 HTTP Authorization。

普通访问不执行旧工作流表迁移。旧 `workflow_jobs` 类型约束不支持校对任务，或旧
`workflow_attempts` 的 outcome / 终态 CHECK 不支持 `cancelled` 时，写入控制命令会明确拒绝。
先保留源库并检查[显式升级路径](../operations/archive-upgrades.md)；支持的固定源在独立新目录转换，
未知布局需专门的读取器和迁移政策。程序不自动删除、重建或迁移现有库，重新安装包不能修改旧表约束。详情见 [显式升级路径](../operations/archive-upgrades.md)。

## 处理已有转录

以下 Linux / WSL 示例中的 `101` 是数据库 `video_part_id`，不是视频 BV 号。
在安全的运行环境设置密钥后执行：

```bash
batch-transcribe workflow proofread --archive-root /srv/bili-archive --part-id 101
batch-transcribe workflow run --archive-root /srv/bili-archive --only-editorial
batch-transcribe workflow status --archive-root /srv/bili-archive
```

可以用 Python 查询视频部分 ID 和已经存储的转录 ID：

```python
import sqlite3

with sqlite3.connect('/srv/bili-archive/archive.db') as db:
    print(db.execute('''
        SELECT p.video_part_id, p.bvid, p.page_index, t.transcript_id,
               t.source_kind, t.language, t.version
        FROM video_parts AS p
        JOIN transcripts AS t ON t.video_part_id = p.video_part_id
        WHERE p.bvid = ?
        ORDER BY p.page_index, t.transcript_id
    ''', ('BV1o24y157iQ',)).fetchall())
```

`proofread` 在计划时固定基础转录、当时可用的字幕、视频标题、模型参数和提示词。
后续出现新的转录或标题变化，不会替换已有任务的输入。参考字幕优先 CC，再选 AI 字幕。
匹配时识别 `Chinese`、`ai-zh`、`zh-CN`、`zh-Hans` 等同类语言标签，原始标签仍保留在快照中。

明确指定转录版本或不用参考字幕：

```bash
batch-transcribe workflow proofread --archive-root /srv/bili-archive \
  --base-transcript-id 201 --reference-transcript-id 202

batch-transcribe workflow proofread --archive-root /srv/bili-archive \
  --base-transcript-id 201 --no-reference
```

显式基础 ID 也允许选择原始字幕版本；默认 `--part-id` 路径要求已有 ASR。
参考转录必须是同一视频部分和兼容语言的字幕，不能引用另一视频的字幕。
相同固定输入与配置重复计划会命中已有任务。修改输入版本或配置会产生新的输入标识。

## 接入本地 ASR 主链路

```bash
batch-transcribe workflow plan --archive-root /srv/bili-archive --part-id 101 \
  --asr-policy all --proofread \
  --model /srv/models/Qwen3-ASR-1.7B-hf \
  --aligner /srv/models/Qwen3-ForcedAligner-0.6B-hf
batch-transcribe workflow run --archive-root /srv/bili-archive
```

先导入音频，任务依赖为 `asr → proofread → render_document`。本仓库不创建采集任务。自动校对固定其 ASR 前置任务成功返回的准确 `transcript_id`，
在首次执行时取已有字幕并冻结；重试不会切换为最新 ASR 或后续到达的字幕。
原始转录发布继续独立执行，不等待校对。

## 大块预算

当前实现使用以下上限和默认值；它们属于项目配置，不代替供应商服务规格：

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `--reasoning-effort` | `high` | 开启思考；可选 low / high / max |
| `--top-p` | 0.95 | 思考模式有效范围 0.95–1.0 |
| `--editorial-model` | `deepseek-flash` | API 模型标识 |
| `--editorial-base-url` | `https://api.deepseek.com` | OpenAI-compatible API 基地址；OpenCode Go 使用 `https://opencode.ai/inference/go/v1` |
| `--context-tokens` | 1,048,576 | 总上下文预算 |
| `--max-input-tokens` | 480,000 | 提示词、基础文字、参考字幕和只读上下文的预算 |
| `--max-output-tokens` | 262,144 | 修订输出预留；不可超过 393,216 |
| `--context-segments` | 2 | 每块前后各保留的只读基础片段数 |
| `--api-timeout` | 1,800 秒 | HTTP 读取超时；每次请求前续租任务 |
| `--thinking-mode` | `enabled` | `enabled` / `disabled`；关闭时不发送 reasoning effort |
| `--temperature` | 不发送 | 仅 `disabled` 模式允许，范围 0–2；enabled 模式显式提供会在本地拒绝 |
| `--max-chunk-regenerations` | 2 | 每块额外物理调用预算，范围 0–4；新任务默认最多 3 次物理调用 |

另预留 16,384 token 的安全空间。当前没有依赖供应商 tokenizer，使用 UTF-8 字节数作为保守
输入估算，同时预留思考、完整段落、来源 ID 和疑点输出长度。它通常会高估文本 token 数，不代表实际计费用量。
实际供应商用量来自响应的 `usage`，保存在调用记录中。
每个冻结块还记录系统提示词、可编辑正文、参考字幕、只读上下文和输出预算的估算值，
并在发送前做本地格式预检；空白和重复标点只生成安全候选，填充词和重复短语只标记，
不由本地规则直接删除语义内容。整块只有安全格式候选时直接生成逐段机械修订，不调用模型；
含有语义疑点或未变化段落的块仍完整走模型校对。

每块尽量包含最多的完整原始片段。可容纳的整篇转录会保持为单块；输出预算不足时仍需分块，
因为 1M 输入上下文不能保证全文修订输出也能装下。参考字幕按片段时间交集匹配；
基础片段恰好归属一个块，邻块上下文只读，未匹配参考字幕记录到校对记录。
单个原始片段超过预算时明确失败，不截断文字。

```bash
batch-transcribe workflow proofread --archive-root /srv/bili-archive --part-id 101 \
  --max-input-tokens 600000 --max-output-tokens 393216
```

服务返回 `finish_reason=length`、空内容、无效 JSON、重复 JSON 字段、未知片段、漏段、乱序、
正文混入标题或时间戳、非相邻来源或未知引用时，任务失败并保留调用证据，不提交完整修订或阅读稿。
新输入冻结 `request_policy=deepseek-v2`。请求采用 JSON object 模式，默认 `thinking={"type":"enabled"}`、顶层 `reasoning_effort="high"`；
可用 `--thinking-mode disabled` 关闭思考，启用思考时 `high` 仍是默认 effort。
enabled 模式 `top_p=0.95`，不发送 temperature；disabled 模式显式发送有效 `top_p=1`，
可用 `--temperature 0.2` 设置 temperature，不发送 reasoning effort。无隐式 HTTP 自动重试。
官方接口在 high 思考模式下将小于 0.95 的 top_p 按 0.95 处理；本地配置直接拒绝无效范围。
temperature 在思考模式下没有效果，因此 enabled 模式默认不发送；需要比较采样差异时只能在
`--thinking-mode disabled` 下显式设置。完整默认值、调优结论和冻结规则见
[本地转录与 AI 校对参数](pipeline-parameters.md)。

官方参考：[模型规格](https://api-docs.deepseek.com/quick_start/pricing/)、
[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、
[JSON 模式](https://api-docs.deepseek.com/guides/json_mode/)。

### OpenCode Go

```powershell
$env:OPENCODE_API_KEY = "your-opencode-api-key"
batch-transcribe workflow proofread --archive-root C:\path\to\archive --part-id 101 `
  --editorial-base-url https://opencode.ai/inference/go/v1 `
  --editorial-model deepseek-v4.1-flash
```

OpenCode Go 的完整端点是 `https://opencode.ai/inference/go/v1/chat/completions`；项目参数填写不含最后 `/chat/completions` 的基地址。模型标识使用 `deepseek-v4.1-flash`。旧的 `/zen/go/v1` 基地址继续用于读取和执行已有冻结任务。

随后执行 `batch-transcribe workflow run --archive-root C:\path\to\archive --only-editorial`。
计划时会冻结 API 地址、模型和预算；改变 CLI 参数只影响新任务，不会修改已冻结任务。
Go 官方文档将该服务定位于编程代理及类似请求，字幕阅读稿校对的适用性需要向供应商确认。
此接入发送相同的版本化兼容请求格式；OpenCode Go 对 JSON 模式、thinking、顶层 reasoning_effort、
temperature、top_p 和最大输出预算的实际支持尚未通过本次修复验证，不能直接视为已经验证的官方 DeepSeek 规格。
参考：[OpenCode Go 文档](https://opencode.ai/docs/en/go/)、[模型目录](https://opencode.ai/inference/go/v1/models)。

可复核的真实保存调用回放、显式付费的小样本参数检查和持久调用上限见
[校对恢复与参数评估](../operations/editorial-evaluation.md)。离线回放不访问模型；
在线检查要求显式 `--allow-paid-calls`，最多四次物理调用，每次输出最多 8192 tokens。
HTTP 接受请求、严格结构通过、参数确实生效与人工核验语义质量必须分别记录。

## 修订与文档

当前规则 `readable-prose-v2` 的目标是让转录可阅读，语句和局部论述关系通顺。
允许删除无意义填充词、口吃重复和半句重启，合并相邻字幕，恢复跨段切开的词，
补齐标点、调整局部语序。保留原话的观点、例子、论据、强调、因果与转折；不生成摘要或扩写。
参考字幕只作证据，不假定一定比 ASR 正确。数字、专名、否定、立场等实质疑点不得猜改。
疑点另存 `issues`，不回退整段为原始口语，周围语句继续整理。
无独立信息的半句重启和未说完插话移入校对记录，正文续接完整意思；不凭空补齐实质内容。

模型输出结构：

```json
{"chunk_id":"输入中的块ID","paragraphs":[
  {"segment_ids":["t2:s0","t2:s1"],"text":"完整、通顺的正文段落。","issues":[]}
]}
```

每个段落关联连续基础片段，所有可编辑片段按顺序出现且恰好一次。程序验证来源覆盖和引用，
不能仅凭结构校验判断语义准确。保存的段落同时包含程序取得的原文和时间范围，供独立审核。
每个输入片段提供 `allowed_issue_refs`，列出本片段及相交参考字幕；段落疑点只能引用其合并片段
允许 ID 的并集。无关引用会失败，不会被程序静默替换成其他证据。

产物路径：

```text
<write-base>/documents/part-<video_part_id>/<revision_id>/ai-draft-v1/
  ai-draft.md
  review.md
```

write base 默认是 archive root；`workflow run --artifact-root PATH` 或
`BILI_ARTIFACT_ROOT` 可指定独立的现有目录，数据库仍留在 archive root。
render 只排队，随后的 run 与 publication create / editorial export 都需要同一目录配置；路径与哈希登记于
`document_artifacts`。详见 [artifact-root.md](../operations/artifact-root.md)。

`ai-draft.md` 只有整理后的正文段落，不含视频标题、话题标题、目录、时间戳、脚注或审核说明。
`review.md` 保存模型思考和采样参数、固定输入、每段全部来源 ID、原文与整理稿对照、
时间范围、回看链接、疑点和未匹配参考字幕，并注明未经人工复核。
源文本按字面转义，不能把字幕中的 HTML 或链接指令直接变成文档行为。
旧规则和旧模板不兼容；需要按当前规则创建新的固定输入和修订，不能把旧结果冒充新稿。

## 失败重试与重新渲染

```bash
batch-transcribe workflow retry --archive-root /srv/bili-archive --part-id 101 \
  --control-state /srv/production-ops/state.json --dry-run --json
# 检查决定后去掉 --dry-run，实际重排符合政策、hold、预算与退避的任务。
batch-transcribe workflow run --archive-root /srv/bili-archive --only-editorial
```

每个通过校验的块在任务拥有的写事务内保存；完整修订也在独立的受保护事务内提交。
事务取得 SQLite 写锁后重新检查 job、lease owner、attempt 编号及未过期租约，
使校验结果与取消决定串行化。进程重启、超时或模型响应无效导致任务失败后，
政策允许的 `workflow retry` 可复用已保存块，只重新请求尚未通过校验的块。
它要求显式控制文件，或在确无库外控制的部署中声明 `--no-control-state`。
模型结构校验失败默认不自动重试；余额、配置和源质量失败要求先修复原因。
精确人工 override 需要一个 job ID 和理由，也不会解除 holds。
完整分类、状态口径和命令见 [工作流失败与恢复](../operations/workflow-failures.md)。
全部块完成并提交完整修订后，校对 job 还须成功结束，渲染依赖才能就绪。
如果请求已到达供应商，但本地尚未保存结果时进程退出，重试可能重复计费；不承诺外部调用 exactly-once。
调用快照包含由输入、块和完整请求计算的稳定指纹。恢复时会先查找同一指纹下已保存且未标记错误的响应，
再执行正常 JSON、来源和结构校验，从而覆盖“响应已落库但检查点尚未提交”的常见崩溃窗口。
租约续期和尝试编号校验阻止过期工作器提交新块或覆盖新的任务尝试。
`workflow retry` 只重排 `failed`，不会恢复 `cancelled`；重复相同输入的校对请求也不会复活已取消任务。

新冻结输入包含 `max_chunk_regenerations` 和请求 policy；每块所有物理调用（首次、再生成、失败后的显式重试）
共用 `1 + max_chunk_regenerations` 的持久化上限。默认 2 表示总共最多 3 次，不是每个 job attempt 再获得 3 次。
`--max-chunk-regenerations 0` 表示该输入每块最多一次物理调用，仍可免费复用已保存响应或已通过的检查点。
worker 在拥有任务租约的短事务内、HTTP 前写入 `editorial_model_calls` 调用预留；未完成或传输失败的调用
也消耗预算。供应商是否收到请求不明时不自动返还额度；进程重启、重排、升级不会重置额度。
本地格式预检和已保存响应的 replay 使用独立 invocation kind，不消耗物理额度；实际 usage 仍保留于原响应。

自动再生成首批只允许 `cross_paragraph_reference`：引用是当前 editable chunk 中存在、但不属于本段的
基础来源或参考字幕 ID。未知、格式错误或只读上下文 ID，以及漏段、乱序、截断、无效 JSON 等错误维持严格失败。
已确认的跨段错误会记录结构化诊断，下一次实际请求在原系统提示词和原 frozen chunk 之后加入明确的诊断反馈，
要求重新生成整个块；不删除疑点、不替换引用、不放宽验证。调用 envelope 记录 `paragraph-reference-v1`、再生成序号、
前一个 `call_id`、诊断和完整实际请求。每次新响应仍须通过完整原始校验才能保存检查点。
耗尽后以 `EditorialRecoveryExhausted` 失败，依赖 render 继续阻塞，人工复核后可显式 repair 创建新身份。

没有 request policy 的历史 snapshot 按 `legacy-v1` 读取，保持旧嵌套 reasoning_effort 请求格式和原始输入身份，
不自动启用诊断再生成。普通默认 replanning 继续复用历史输入和终态；需要修正参数契约或启用新预算时，使用
`--refresh-input` 或 `repair-proofread` 显式生成新输入。生产维护对已证明跨段引用的历史失败可按其有界 job 重试政策
重新请求原 frozen 输入，不改变原请求语义。维护先检查新输入的物理预算，不会以 job 重排乘出另一份块预算。

更新提示词后，旧 job 仍使用自己的冻结输入。需要采用当前提示词、调整分块或更换
经过复核的转录版本时，使用显式恢复入口：

```bash
batch-transcribe workflow repair-proofread --archive-root /srv/bili-archive --job-id <failed-proofread-job-id>
# 异常源转录经复核并作为新版本存储后，显式指定该版本：
batch-transcribe workflow repair-proofread --archive-root /srv/bili-archive --job-id <failed-proofread-job-id> --base-transcript-id <corrected-transcript-id>
```

该命令只排队，使用当前提示词重新冻结输入，并建立新的 proofread/render 依赖链。
旧 job、attempt、输入、API 响应、已保存块和原渲染任务保持原状态；原渲染任务仍受旧失败依赖阻塞。
新任务的 payload 保存 `repair_of_job_id`，相同恢复请求幂等。
替换来源必须属于原分 P，参考字幕沿用原冻结版本；当前输入身份与旧输入相同则拒绝恢复，应使用普通 retry。
原输入已保存的块不会移植到采用新提示词的任务。命令不修改静音例外或任何 retry hold。

普通新输入默认每块最多 256 段、20,000 字符；恢复默认 128 段、10,000 字符及
`reasoning_effort=low`，可用 `--max-chunk-segments`、`--max-chunk-chars` 和
`--reasoning-effort` 显式调整。输入和输出 token 预算仍独立生效，单段无法满足预算时明确失败。
最大输出预算包含供应商计入的推理 token；提高预算不能解决模型持续重复的情况。

调用模型前检查 ASR 单段的异常重复：至少 1,000 字符、密度超过每秒 100 字符，
且同一四词组合重复至少 24 次时，以 `editorial_source_repetition` 停止。
该检查保留全部原文，记录来源 ID、段序号、时间、长度和重复计数；需复核或重新识别来源后再恢复。
长文本或普通重复本身不触发该检查。它是保守的故障拦截，不代替音频质量评估。

跨段证据引用仍严格拒绝，attempt 的 `result_json.diagnostic` 记录段落、issue 序号、
非法引用计数和摘要，以及 `call_id`/`chunk_id`；原响应保存在 `editorial_model_calls` 供核对。
截断诊断记录输出/推理长度和供应商提供的 token 用量，不把响应正文写入诊断。

读取成功校对尝试的 `result_json`，或查询 `editorial_revisions`，取得 `revision_id` 后：

```bash
batch-transcribe workflow render --archive-root /srv/bili-archive --revision-id <revision_id>
batch-transcribe workflow run --archive-root /srv/bili-archive --only-editorial
```

重渲染不需要 API 密钥，不调用模型，可修复已删除的 Markdown。
相同已保存修订和同一已登记模板生成相同字节；按保存身份选择 `ai-draft-v1` 或 `ai-draft-v2`。
未知模板拒绝，排版改变需要新的版本和实现，不能改写历史 renderer 的字节算法。两份文档先在临时文件中编码、fsync 并计算摘要；
随后在同一任务所有权事务内执行最终替换并登记路径与 SHA-256。
取消若先提交，最终替换被拒绝并清理本次临时文件。相同修订和模板对应的 render job
已经取消时，重复 `workflow render` 保持取消状态。
文件系统与 SQLite 不是跨介质原子事务，文件替换或数据库提交异常后仍需核对文档与登记摘要。

## 协作式取消

先列出 job ID，再明确选择需要取消的校对或渲染任务：

```bash
batch-transcribe workflow status --archive-root /srv/bili-archive --jobs
batch-transcribe workflow cancel --archive-root /srv/bili-archive --job-id JOB_ID --job-id ANOTHER_JOB_ID
```

queued / running 任务变为 `cancelled`；成功、失败或已经取消的任务返回 noop。
取消不级联：取消校对后，其依赖渲染仍为 queued，并在 status 中显示 `blocked_by_cancelled`。
需要取消后续任务时，应明确选择它们的 job ID。

取消在 checkpoint 和提交守卫处生效，不保证立即中断已经发出的模型请求，也不能撤销供应商计费。
模型请求返回后，实际响应和错误诊断可以继续保存到 `editorial_model_calls`；
它们是调用证据，不能在任务已取消后被接受为新块、完整修订或渲染产物。
在取消前已经提交的块、修订和文件保留，取消不会回滚历史结果或删除已完成文档。
若短提交事务先取得锁，取消等待它提交；因此可能出现结果已经提交、但 job 尚未 finish 时被取消的情况。
状态、依赖和旧库限制见 [失败与恢复](../operations/workflow-failures.md)。

## 完整版本、审核与发布

AI 工作流只生成 `ai-draft.md` 与原文对照、疑点所在的 `review.md`。后者是校验参照稿，
不代表人工审核通过。公开 release 与未发布预览都可携带对应 AI revision 的原始参照稿，
网站通过独立校验参照入口展示原文、整理稿、疑点和回看链接。参照稿针对 AI 初稿，
后续人工编辑不改写它；完整审核记录和请求配置仍在私有审阅包。明确选择 revision 创建完整 edition 后，
标题、正文、摘要、标签、冻结来源、整理归属和编辑说明共同决定内容 SHA-256。

```bash
batch-transcribe publication create --archive-root /srv/bili-archive --revision-id REVISION_ID --actor EDITOR
batch-transcribe publication show --archive-root /srv/bili-archive --edition-id EDITION_ID --format json
batch-transcribe editorial export --archive-root /srv/bili-archive --revision-id REVISION_ID --edition-id EDITION_ID --out private-review
```

审核指定准确 edition 与完整内容哈希；批准后仍需显式 `publication publish` 才生成固定
`publish.md` 并切换有效 release。创建、修改或批准 B 不影响已经公开的 A。
`publication export` 只导出验证通过的有效 release，`publication export-drafts` 只导出当前且从未发布的 edition；
两者的 catalog 都登记正文和原始 `review.md` 的路径与字节哈希：无 origin profile 且全部 content v1
时为 catalog v2，存在 content v2 或显式 origin profile 时为 v3。`publication withdraw` 清除公开指针，
保留内部内容与审核历史。公开 release、未发布预览和私有审阅包的输出目录严格独立；历史合法版本按保存身份校验，未知契约拒绝。
完整命令、CAS、目录恢复及 JSON 契约见 [稿件与公开导出契约](../contracts/registry.json)。

## 数据与离线验证

| 表 | 内容 |
| --- | --- |
| `editorial_inputs` | 完整固定输入、来源 ID、配置、提示词正文与散列、分块及引用 |
| `editorial_job_inputs` | 工作流任务与固定输入的绑定 |
| `editorial_model_calls` | 所属工作流尝试、请求、实际响应、用量、错误码和时间 |
| `editorial_chunk_results` | 每个块通过校验后的完整段落、来源 ID、原文和疑点 JSON |
| `editorial_revisions` | 全部块的完整修订、内容标识、`ai-unreviewed` / `needs-review` 质量状态 |
| `document_artifacts` | 修订、模板、文件路径和散列 |

没有另建调度器；任务和终态仍归属于 `workflow_jobs` / `workflow_attempts`。
段落和块信息保存在结构化 JSON 中，当前实现不额外建 `editorial_changes` / `editorial_blocks` 表。

```bash
python -m pytest tests/test_ai_editorial.py tests/test_workflow_control_plane.py tests/test_workflow_cancellation.py tests/test_publication.py tests/test_publication_export.py -q
```

测试使用合成转录、模拟模型和被拦截的 HTTP 响应，不连接 DeepSeek 或 B站，不需要 API 密钥。
覆盖输入固定、上下文和输出预算、语言标签、结构校验、识别疑点、HTTP 失败、逐块恢复、
ASR 独立依赖、租约、Markdown 转义及字节一致的重新渲染。
取消测试使用独立 SQLite 连接触发模型响应晚到、渲染暂存后取消和短写事务竞争，
验证审计可保留、成功结果被拒绝、已取消任务不复活以及旧 attempt 约束被拒绝。
阅读导出测试验证只读连接、内容快照、审核与人工版本、摘要拒绝及受管理文件清理。
当前离线验证覆盖跨段成句、疑点不回退正文、来源覆盖与只读上下文边界、纯正文输出，
以及 high 思考和 top_p 参数的请求与快照。离线测试不连接 DeepSeek 或 B 站；
本文的验证依据是这些离线契约测试，不代表已经验证真实供应商调用或阅读站页面。
真实素材的人工回听和逐句准确率评估仍需单独记录，不能把结构校验当作语义质量结论。
