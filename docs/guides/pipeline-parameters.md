# 本地转录与 AI 校对参数

参数以当前源码及 CLI 帮助为准。任务规划时冻结配置；之后修改环境变量不会更改已排队任务。

## 输入和工作流

```sh
batch-transcribe archive init --target-root archive
batch-transcribe input import --archive-root archive --manifest inputs.json
batch-transcribe workflow plan --archive-root archive --all-parts --proofread
batch-transcribe workflow run --archive-root archive --role asr
batch-transcribe workflow run --archive-root archive --role editorial
batch-transcribe workflow run --archive-root archive --role cpu
```

处理链是已登记音频 → ASR → 质量检查 → AI 校对 → 文稿渲染；转录包独立生成。输入清单见 [示例](../../examples/inputs.json)，选择规则见 [批量选择](workflow-selection.md)。

## ASR

| 设置 | 代码默认值 | 入口 |
| --- | --- | --- |
| 模型 | `Qwen/Qwen3-ASR-1.7B-hf` | `--model` / `BILI_ASR_MODEL` |
| 对齐模型 | `Qwen/Qwen3-ForcedAligner-0.6B-hf` | `--aligner` / `BILI_ASR_ALIGNER_MODEL` |
| 设备 | `cuda` | `--device` |
| 目标分块 | 180 秒 | `--chunk-seconds` |
| 输出预算 | 每秒 8 token，最小 256 | `--tokens-per-second` / `--min-new-tokens` |
| 热词 | 空 | `--hotword`，仍需证据守卫 |
| ASR / 对齐批大小 | 1 / 1 | `--asr-batch-size` / `--aligner-batch-size` |
| 对齐解码 | `default` | `--alignment-decoder`，可显式选择 `monotone-max-score` |
| 覆盖策略 | `full-span` | `--coverage-policy`，可显式选择 `quiet-edges` |
| 离线加载 | 开启 | `--offline` / `--no-offline` |

来源：`src/bili_asr/asr/config.py`、`cli/workflow.py` 和 `constants.py`。扩展策略不会追溯改变历史 profile；覆盖率也不代表文字准确率。不要排序、钳制时间戳或降低阈值掩盖质量问题。完整说明见 [参数快照](../asr/asr-configuration.md)、[有界批处理](../asr/asr-batching.md) 和 [输出策略](../asr/asr-output-policy.md)。

## AI 校对

默认 endpoint、模型标识和请求策略由 `src/bili_asr/editorial.py` 决定，不作为供应商当前产品规格的证明。`--editorial-base-url`、`--editorial-model`、`--context-tokens`、`--max-input-tokens`、`--max-output-tokens`、`--thinking-mode`、`--reasoning-effort` 和 `--chunk-preset` 在新任务中冻结。

用 `workflow proofread-many` 在一个事务内规划已有转录；任一目标不合格时回滚整批。预算按实现中的保守估算限制，实际计费用量来自响应。API 失败和未知发送状态保留证据，不隐式重放付费请求。详见 [AI 校对](ai-proofreading.md)、[预算](../operations/editorial-budget-and-comparison.md) 和 [恢复](../operations/editorial-recovery.md)。

## 批次和生产控制

持久批次需显式安装扩展，冻结既有任务成员及 payload。暂停只阻止新领取，已领取任务沿原租约完成；恢复不清除失败、retry holds 或未知发送状态。`production` 管理配置、worker 和离线备份；配置变更只影响新规划任务。查看 `batch-transcribe production --help` 及 [批次说明](../operations/workflow-batches.md)。
