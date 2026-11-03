# ASR 参数快照与运行诊断

本页描述源码 `378f415ac5bf2a47de9137ee593f201e86827d43` 的 ASR 配置冻结、执行与诊断。规划时确定配置，任务执行时恢复自己的快照；后续环境变量变化不修改已有任务。质量实验见 [公开样本测试](../evidence/asr-public-samples-2026-10-08.md)，其中的测试结果保留各自的源码与样本身份。

## 1. 数据与执行关系

```mermaid
flowchart LR
    CLI["CLI 参数 + 规划时环境"] --> Config["验证有效配置"]
    Config --> Profile["不可变 profile + SHA256"]
    Profile --> Job["ASR job 引用 profile_id"]
    Audio["音频对象与哈希"] --> Job
    Ref["规划时固定参考转录 ID"] --> Job
    Job --> Run["事务外识别与对齐"]
    Run --> Text["转录版本与逐段文本"]
    Run --> Evidence["每次运行的诊断 JSON"]
    Text --> Attempt["同事务写入 acquisition_attempt"]
    Evidence --> Attempt
    Attempt --> Publish["发布任务与归档产物"]
```

任务执行从已存储 profile 重建配置，不再读取当时的 ASR 环境变量。相同 profile key 与完整配置复用同一 profile；修改分块、超时、热词、对齐版本或生成预算等参数会产生不同配置哈希。不同 profile 的任务可以独立规划。

文本版本仍按现有内容规则去重。因此新配置产生新的任务和运行证据，但输出逐段文本完全相同，可能复用既有 transcript。诊断以 `run_id + video_part_id` 保存，避免内容去重吞掉实验记录。

## 2. 参数入口与默认值

规划命令中显式提供的参数优先于环境变量；未提供的参数取规划时环境或默认值。已有任务保持自己的快照。

| CLI 参数 | 环境入口 | 默认或语义 |
|---|---|---|
| `--model` | `BILI_ASR_MODEL` | Qwen/Qwen3-ASR-1.7B-hf |
| `--model-revision` | `BILI_ASR_MODEL_REVISION` | 未固定；建议可复核实验固定 commit |
| `--aligner` | `BILI_ASR_ALIGNER_MODEL` | Qwen/Qwen3-ForcedAligner-0.6B-hf |
| `--aligner-revision` | `BILI_ASR_ALIGNER_REVISION` | 独立版本，不继承新的 ASR revision |
| `--device` | `BILI_ASR_DEVICE` | cuda |
| `--language` | `BILI_ASR_LANGUAGE` | 自动识别 |
| `--chunk-seconds` | `BILI_ASR_CHUNK_SECONDS` | 180 秒目标分块长度 |
| `--inference-timeout` | `BILI_ASR_INFERENCE_TIMEOUT_SECONDS` | 1800 秒 GPU 子进程硬时限 |
| `--hotword`，可重复 | `BILI_ASR_HOTWORDS` | 空；CLI 列表替换环境列表，仍经过证据守卫 |
| `--model-id` | `BILI_ASR_MODEL_ID` | 本地 checkpoint 的声明身份 |
| `--offline / --no-offline` | 无 | 离线为 true |
| `--tokens-per-second` | 无 | 8 |
| `--min-new-tokens` | 无 | 256 |
| `--second-pass-cache / --no-second-pass-cache` | 无 | 第二遍关闭生成内 KV cache |
| `--model-dtype` | `BILI_ASR_MODEL_DTYPE` | bfloat16；也可显式选择 float16 |
| `--aligner-dtype` | `BILI_ASR_ALIGNER_DTYPE` | bfloat16；也可显式选择 float16 |

识别与对齐模型及各自 processor 都接收自己的 revision 和 `local_files_only`。本地目录必须预先准备好文件；设置 offline 时，缺失文件会失败。允许联网加载时，应显式使用 `--no-offline`。

模型与对齐器分别冻结精度，默认均为 BF16；改成 FP16 会进入带版本的 `precision` 扩展并改变配置哈希。16 kHz 重采样与当前分块算法不通过本页参数调整。分块器在目标附近寻找低能量边界，因此目标秒数不是严格的最大输入长度；应遵守 checkpoint 的实际输入约束。

相邻块批处理默认关闭（ASR 和 aligner 的 batch size 均为 1）。显式的批大小、音频秒数、输入字节和 token 预算进入 `batching` 扩展，详见 [批处理](asr-batching.md)。attention、KV cache 和编译策略使用独立的 `runtime_strategies` 扩展，详见 [运行策略](asr-runtime-strategies.md)；这些配置变化不能靠修改旧任务的环境变量应用。

音频在项目内完成解码、转单声道和重采样后，以 16 kHz float32 波形数组直接传给识别与对齐 processor。短尾仍按原规则填充。这样避免 processor 再按文件路径调用可选解码后端，也省去逐块临时 WAV 写入与 PCM16 量化；不能承诺与此前经过量化的输入产生逐字相同结果。

生成预算由有效 mel 帧估计音频秒数，再取 `max(min_new_tokens, int(tokens_per_second × 秒数))`。它是输出上限，不是字数目标。达到上限且未确认 EOS 的块会标记截断风险，不自动缩短块或重识别。

热词二遍使用独立的生成调用；没有把第一遍 KV cache 传给第二遍。第二遍关闭 cache 是保留的基线，可通过新参数开展对照实验。空候选不会触发第二遍。

项目使用策略为热词一般不考虑，默认保持空，不列入常规调优计划。上文记录已有接口与执行语义。已记录样本可作为模型组合、中文任务显式 Chinese、180 秒分块与现有生成预算的参考，但尚未证明这些设置对所有视频最优。

CPU runner 在同一 workflow handler 中可复用模型。CUDA / ROCm 的 `workflow run` 和 `workflow supervise` 默认使用 `--gpu-session persistent`，在同一会话中顺序处理任务并复用模型；完整配置或模型绑定变化时重建会话。`--gpu-session oneshot` 每次请求后关闭子进程，可用于故障定位和回退。推理期限、取消、租约丢失和排空期限仍受监督，会话失败后清理并在后续请求重新建立。CPU 同步推理没有同等的硬超时终止边界。角色、心跳和进程协议见 [ASR worker](asr-workers.md)。

## 3. 规划与查询

```powershell
bili-asr workflow plan --part-id 42 --profile-key chunk-60 --chunk-seconds 60 --device cuda
bili-asr workflow run --limit 20
bili-asr workflow asr-evidence --run-id RUN_ID --part-id 42
```

`RUN_ID` 是 ASR acquisition run 的 ID。成功任务的结果保存在 `workflow_attempts.result_json`，不是 `workflow_jobs` 的字段；`run_id` 也不是 workflow job_id 或 workflow attempt_id。可以通过 SQLite 查找成功的 ASR 尝试：

```sql
SELECT j.job_id, j.video_part_id, a.attempt_id,
       json_extract(a.result_json, '$.run_id') AS acquisition_run_id,
       json_extract(a.result_json, '$.quality.status') AS quality_status
FROM workflow_jobs AS j
JOIN workflow_attempts AS a ON a.job_id = j.job_id
WHERE j.kind = 'asr' AND a.outcome = 'succeeded'
ORDER BY a.finished_at, a.attempt_id;
```

`asr-evidence` 输出 JSON，找不到对应记录返回退出码 1。该命令使用 READ 访问已有归档，并只读检查契约；查询不会初始化数据库、补建表或刷新视图。

## 4. 诊断内容与解释

每次成功保存转录时，诊断与转录、attempt 在同一事务提交。记录包括 profile ID、配置哈希、固定参考转录 ID、音频依赖结果、provenance 以及实际执行的每一遍：

- 运行环境：Python、平台、相关包版本、CUDA / HIP 构建信息、实际模型 dtype、可取得的模型 commit hash。
- 阶段耗时：模型加载、音频准备、分块、解码、对齐与本遍总耗时。两遍运行合计包含第一遍成本；阶段值之间存在包含关系，不应再与 total 相加。
- 逐块证据：原音频开始和结束时间、原始识别文字、检测语言、生成 token 数与上限、EOS 观察、对齐时间区间并集、最大未对齐间隔及非法时间单位计数。

对齐区间取 raw aligner units，早于字幕 cue 合并，重叠区间只计一次。合法区间有 50 ms 末端容差并裁到输入长度；短尾块的对齐输入可能包含填充，其长度用于边界检查。无对齐间隔可能是正常静音；并集不是“语音识别完整率”。旧 coverage 表保留首尾跨度指标语义。

| quality.status | 含义 |
|---|---|
| `not-evaluable` | 没有足够的已完成诊断或未解码有效音频 |
| `needs-review` | 最终一遍出现已知风险标记 |
| `no-known-risk` | 最终一遍未触发现有规则；没有人工正确率保证 |

现有风险标记为 `empty-output`、`empty-alignment`、`invalid-alignment`、`token-limit-reached` 与 `span-coverage-short`。中间块为空，即使首尾跨度看起来完整，也会触发复核。没有可观察 EOS 时字段保持 null；不伪造完成证据。所有状态都保留 `human_reviewed=false`。

质量状态目前供查询与实验使用，尚不自动阻止发布或改变转录选择策略。第一遍记录供分析，最终质量判断使用最后一遍。

## 5. 存储兼容与边界

`workflow_asr_profile_configs` 保存 schema version 2 的完整配置；`transcript_asr_evidence` 保存 schema version 1 的运行证据。普通 READ/WRITE 访问只检查已有契约，不执行 DDL。新空归档通过显式 `archive init` 安装当前运行契约；已登记的旧库组合通过 [显式升级](../operations/archive-upgrades.md)在独立目标转换。未知或不兼容的旧式数据库会明确拒绝，不通过打开归档自动补表，也不重写旧 profile 或历史转录。

旧 profile 没有扩展快照时，核对旧配置哈希，保留曾经共用 ASR / aligner revision 的执行语义，并使用旧默认分块、热词和超时。新快照哈希不符或扩展快照被删除时拒绝执行，避免静默回退到默认值。

完整诊断 JSON 绑定于成功转录。模型异常、空整体输出、硬超时及 lease 丢失走失败 attempt 与采集运行记录；没有持久化失败运行的全部中间块诊断。当前没有持久化按块检查点，重试按完整任务执行；常驻 GPU 会话只复用模型，不把上一任务的诊断、热词或输出带到下一任务。VAD、人工基准 CER 与峰值显存不由 `asr-evidence` 质量状态证明；独立 profiling 和评测的范围见 [诊断工具](asr-profiling.md)、[GPU 验证记录](../evidence/asr-gpu-validation-2026-10-11.md)与 [公开样本测试](../evidence/asr-public-samples-2026-10-08.md)。

CPU BF16 链路样本的实际测量与限制见 [公开样本记录](../evidence/asr-public-samples-2026-10-08.md#同日单条-cpu-链路样本)。后续 GPU 与精度实测单独保存在 [GPU 验证记录](../evidence/asr-gpu-validation-2026-10-11.md)，不把旧 CPU 样本称为当前所有配置的验收。

## 6. 后续实验

先固定音频哈希、checkpoint commit、人工转写和规范化规则，默认保持空热词，优先在真实项目音频上对照 120 / 180 秒分块、自动语言 / Chinese。记录 CER、边界漏字与重复、异常空块、冷启动和模型复用 RTF、峰值显存及关键术语人工检查结果。热词及其第二遍 cache 不列入常规调优计划。

确认测量结果后再决定默认值。最终文字单次对齐、VAD 辅助复核和持久化分块检查点仍属于后续设计；已有的受监督常驻 GPU 会话及任务内准备见 [ASR worker](asr-workers.md)。

首批公开样本语言、分块与静音对照已完成，完整方法、结果和命令见 [公开样本与调优依据](../evidence/asr-public-samples-2026-10-08.md)。该批没有显示缩短分块的准确率收益，并观察到纯静音生成文字；下一步优先验证语音条件诊断。公开朗读与人工拼接样本不能替代真实项目长视频基准。
