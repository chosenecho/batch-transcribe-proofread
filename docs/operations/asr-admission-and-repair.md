# ASR 准入与显式修复

`asr-admission/v1` 区分推理完成、结构可用、证据不足和发布结果。成功执行
模型并不证明转录可用于校对。workflow 在保存原始推理后，使用以下状态：

| 状态 | 含义 | 下游准入 |
| --- | --- | --- |
| usable | 必要时间线与覆盖测量通过 | 允许发布和校对；仍不宣称语义准确 |
| unusable | 倒退、越界、非法时间或正文、覆盖矛盾 | 阻止自动发布与付费校对 |
| needs-review | 实测覆盖不足或 runner 要求复核 | 阻止自动下游，需要来源修复和重验 |
| unknown | 缺少解码/覆盖证据 | 不视为合格；历史数据需显式重验 |

新建或重新执行的 `workflow publish` 优先使用 ASR 主稿，源站字幕用于辅助
识别与校对。发布时再次核验 ASR 准入：只有 `usable` 可以成为主稿；
`unknown`、`needs-review` 或 `unusable` 则退回已有源站字幕，人工字幕优先于
机器字幕。没有字幕可退时，发布保留原质量错误，不发布未通过准入的 ASR。
没有 ASR 的分 P 仍可直接发布字幕。此排序不代表 ASR 的语义准确率高于人工字幕。

升级代码不会重写已有 release、publication 或冻结输入，也不会自动补建发布任务。
显式重新执行 `workflow publish` 时，符合准入的 ASR 可以取代此前的字幕主稿，
新发布的 `transcript_id` 和 `source-metadata.json` 将反映实际选中的 ASR。
刷新已有 publication 的任务仍固定使用原 transcript，不按新排序切换来源。

起始时间须保持输出顺序单调，允许起点相等及合法重叠。覆盖是已有 runner
测量的输出跨度，不是语音区间之和；正常静音间隙不构成缺陷。时间边界采用
已测解码长度，缺少时才使用已探测容器长度，50 ms 是现有对齐测量容差。
不会为通过检查排序、clamp、丢段或放松 WebVTT。零段/非法段也不证明静音，
`confirmed_silence` 保持未知。

质量准入也检查最终推理 pass 的原始对齐 unit 起点顺序。即使合并后的 cue
区间合法，内部起点倒退仍记录 `backward_alignment_units`，并以
`asr_alignment_unresolved` 拒收；起点相等及合法重叠不受影响。runner 同时
检查 `zero_duration_alignment_units`，同样以 `asr_alignment_unresolved` 拒收，
并给出 `invalid-alignment` 标记。私有评估保留有界的原始文本/unit 证据。
不会通过字幕合并、排序或修改时间掩盖问题，也不会据此判断源音频无声。

断点缓存 v2 同时保存已完成 chunk 的诊断、风险标记和私有对齐证据；续跑时
将它们纳入最终 pass，并标记 `reused_from_checkpoint=true`。旧 v1 缓存或缺少
完整诊断的缓存不能证明输入可用，需重新推理其前缀。恢复后仍有未匹配文本或
倒退或零时长 unit 的输入继续拒收，不因重启获得健康状态。

新推理写入现有 `workflow_quality_assessments`，保存 policy、run、job、attempt、
原始 segments、模型/配置/音频身份、diagnostics 和覆盖证据。可表示的转录照常
写入不可变版本；NaN/Inf、零时长等不能进入严格的 `transcript_segments`，完整
原始输出仍在评估证据中，非有限数值使用明确 JSON 数字标签。acquisition run
记录 inference complete，workflow attempt 记录 `asr_quality_unusable` 或
`asr_quality_unknown`。这两种错误不自动重试；失败依赖继续阻止自动校对。
手动选择被冻结的旧输入时，校对 worker 在模型调用前再次只读检查来源。

评估保存使用 job 的 lease/cancellation 事务屏障，转录与评估原子提交。内容去重
仍保留每个 run 的评估；自动链固定使用自己的 run，不能从后来的健康 run 借用
质量。来源读取不修改原始证据、attempt、hold 或旧出版物。

```bash
bili-asr workflow asr-quality --archive-root /srv/archive --run-id RUN --part-id 1
bili-asr workflow asr-quality --archive-root /srv/archive --transcript-id 2474
bili-asr workflow asr-quality --archive-root /srv/archive --run-id RUN --part-id 1 --include-evidence
```

默认查询仅输出诊断，不含来源文本。`--include-evidence` 显式读取完整私有推理
证据，应保留在受控本地位置。历史没有评估记录时，按存储的原始顺序只读评估；
缺少可核验测量保留 unknown，不能把视频元数据时长冒充解码证据。

修复前核验原音频 SHA-256、ASR/aligner revision、配置及倒退段前后的 alignment。
准备 JSON `AsrProfile`，例如：

```json
{"profile_key":"validated-alignment-repair","model_name":"Qwen/Qwen3-ASR-1.7B-hf","model_revision":"verified-asr-revision","aligner_revision":"verified-aligner-revision","device":"cuda","chunk_seconds":90.0}
```

```bash
bili-asr workflow repair-asr --archive-root /srv/archive --job-id FAILED_ASR_JOB \
  --profile-config /srv/repair-profile.json --reason 'original audio verified; alignment revision repaired'
bili-asr workflow repair-asr --archive-root /srv/archive --transcript-id HISTORICAL_TRANSCRIPT \
  --profile-config /srv/repair-profile.json --reason 'missing decoded evidence; fresh inference over original audio'
```

这建立带 `repair_of_job_id` / `repair_of_transcript_id` 的新 ASR job，并冻结配置。
已证实不可用的 job 必须改变模型/对齐配置；unknown 的 job 可以在原配置下执行
新推理补测。历史 transcript 入口同样冻结显式配置并重新推理，不能“确认一下”直接补写
合格状态。新内容生成新版本；内容相同则复用原内容身份并追加独立 run 证据。
原始失败、原版本、冻结 AI 输入和外部 holds 均保留；相同修复请求去重。任务
入口修复可继承原自动校对配置，新推理通过后才释放自己的依赖。直接 transcript
重验完成后，显式选择其通过评估的版本执行 `workflow proofread` 或
`workflow repair-proofread --base-transcript-id ...`。不自动清除旧任务的 hold。

先用 `workflow asr-quality` 检查新 run，再检查新 job 及出版结果。profile 变化本身
不证明修复成功；若再次不合格，保留新失败并继续检查原音频/对齐。部署新代码
会阻止缺证据的旧 ASR 消耗 AI 请求，升级前先查询、安排必要的原音频重验。
