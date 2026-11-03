# 单次 AI 校对样本的字段与证据

正式工作流已经保存任务、尝试和模型调用记录。直接调用客户端的历史探针可能只有独立账本，不能补造生产任务编号，也不能把事后检查写成调用前准入。本工具为这种单产物、单 chunk 探针生成独立证据包，全程离线，不读取 `.env`，不发起模型请求。

```powershell
python scripts/package_editorial_sample.py <sample-directory> --output <new-package-directory>
python scripts/package_editorial_sample.py <package-directory>
```

源目录需要保存 `source-private.json`、`prepared-private.json`、`validated-revision-private.json`、`calls-private.db`、`ai-draft.md`、`review.md`，以及按账本序号保存的 `call-N-request-private.json`、成功调用的 `call-N-envelope-private.json` 和 `call-N-response-private.bin`。可选的 `call-N-transport.json` 和失败调用留下的响应字节也会保留。账本字段为 `call_id, ordinal, request_json, response_json, outcome, error_json, started_at, finished_at`。序号必须连续；无法关联账本的调用文件会报错。

输出包含原文件的逐字节副本和以下带版本号的 JSON：

- `manifest.json`，契约 `editorial-sample-evidence/v1`：输入与修订 ID、模板版本、完整视频/分 P/转录元数据、调用关联、规范化 Token 用量、质量状态，以及每个文件的用途、相对路径、字节数和 SHA256。
- `source-lineage.json`，契约 `editorial-source-lineage/v1`：逐段原文与校对稿的关联，明确区分声明来源与相邻比较内容。
- `issue-review.json`，契约 `editorial-issue-review/v1`：原模型疑点的原样副本，以及独立审校字段。初始值是 `unclassified / unassessed / open / unlocated`，审校者为空。

用 `--annotations <json-file>` 提供审校条目数组，每条必须包含 `issue_id, issue_type, severity, status, source_spans, reviewer_kind, reviewer, reviewed_at, note`。`reviewer_kind` 为 `human` 或 `agent`，时间必须含时区。类型包括识别、名称、数字、日期时间、单位、无法辨识、来源支持、遗漏和语句通顺性。疑点 ID 从修订 ID、段落序号、疑点序号和原始疑点内容生成。字段枚举见对应的 JSON Schema。

`source_spans` 每项包含 `segment_id, start_char, end_char, quote`，采用从零开始的 Unicode 码点索引，左闭右开。位置必须逐字匹配冻结的源文本，且只能引用所属段落或原疑点声明的证据。没有可靠位置时保留空数组，不能猜测。审校文件不会扩展原始模型响应的三字段疑点结构，也不会改变冻结输入、付费请求、原始响应或历史修订 ID。

统计保留 `prompt_tokens, completion_tokens, total_tokens, cached_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, reasoning_tokens`。其中 `cached_tokens` 来自 `prompt_tokens_details.cached_tokens`；供应商提供的独立 `prompt_cache_hit_tokens` 优先，否则使用嵌套缓存数。缺失或非法计数保留为 `null`，不会把未知值补成零，也不会推算总数或缓存未命中数。

可选 `producer-quality-posthoc.json` 保存只读导出的 `observed_at, transcript_id, content_sha256, rows`。`rows` 包含 `transcript_asr_evidence`、`transcript_coverage_attestations`、`workflow_quality_assessments` 的原始行，证据行的 `evidence_json` 保持字符串。工具只在唯一生产 run 与唯一覆盖率证据能够关联、且没有需由正式工作流解释的旧策略评估时，按当前 `asr-admission/v1` 事后测量；其他情况保留 `unknown`。它不会写回生产数据库。事后 `usable` 只描述源 ASR 的结构与覆盖率，不代表 AI 文本语义准确。

探针未记录的 `invocation_kind`、恢复元数据和调用前门禁会明确标为未知或未记录；任务和尝试编号为 `null`。请求指纹及输入/chunk 关联注明由已验证请求重建。所有输出保持 `needs-review`、`semantic_status=unverified` 和 `review_required=true`，即便没有模型疑点也如此。

验证会重放原始模型响应的结构校验，核对源元数据、冻结请求参数、文稿渲染、疑点原文与定位，并验证文件清单。证据包是私有材料，包含原文和模型响应，应放在 Git 忽略的目录中。SHA256 用于完整性校验，不构成来自供应商或审校者的数字签名。
