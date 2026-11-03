# 对齐解码与近静音首尾的显式策略

默认仍使用模型 processor 的对齐解码和完整音频首尾跨度覆盖率。以下两个选项只在新建 profile 时显式启用，可以分别使用：

```sh
bili-asr workflow plan --part-id 42 --profile-key qwen3-output-policy-v1 \
  --alignment-decoder monotone-max-score --coverage-policy quiet-edges
```

规划命令把选项冻结到 profile v2 的 `output_policy` 扩展（`schema_version: 1`）。省略选项的历史配置保留原始字节和 digest；新配置有新的身份，不会改写旧任务、失败记录或重试 hold。修改策略也会使旧 chunk checkpoint 不再匹配。Python 使用方可在 `ASRConfig` 显式设置同名字段。

## 对齐解码

Qwen3 ForcedAligner 为每个词的开始、结束分别输出时间类别分数。独立 argmax 可以产生倒退或零时长；processor 的后处理也可能把原本不同的两个边界收敛到同一时刻。

`monotone-max-score` 使用模型原始分数做动态规划，最大化整条路径的分数和，约束为 `start_i < end_i <= start_(i+1)`。时间取自模型已有类别，步长读取 processor；只允许实际音频内的类别，短块为推理补的零不计入音频时长。它不会排序已输出的时间戳、截断边界或插值补齐。串行、原生 batch 和 batch 回退使用同一解码函数。

没有正时长路径、分数非有限、词数与时间行数不匹配，或动态规划格点超过 1600 万时直接拒绝。原始 processor 预测、独立 argmax 的零时长/倒退计数、模型分数摘要、改变的位置数和分数损失随 chunk 的 `alignment_decode` 记录。原预测是私有诊断，最多 512 单元、每单元 128 字符、合计 8192 字符；截断会显式标注。完整分数矩阵通过 SHA-256 标识，不直接写入归档。

这保证结构上的有序正时长，不证明词级边界或文字语义正确。未匹配文字、额外对齐单元、输出截断以及出版格式等原有检查继续生效。

## 近静音首尾证据

`quiet-edges` 在实际解码、转为单声道 16 kHz 的完整波形上测量，固定幅度阈值为 −80 dBFS。仅音频开头和结尾连续不超过该阈值的样本可以解释首尾覆盖缺口；普通低声、音乐和内部停顿仍在窗口内。这不是语音活动检测，也不修改模型输入或 cue 时间。

原有 `decoded_s`、`produced_s`、`coverage`、`coverage_min`、`coverage_short` 仍按完整音频计算、保存。额外的 `quiet_edges` 保存采样率、样本总数和有效窗口两端的样本位置；判断时检查方法版本、固定阈值、数据类型、原始时长和 cue 边界，再用 cue 跨度与测量窗口的交集重新计算比值。仍要求覆盖达到原来的 0.97。

只有本次运行显式启用策略且证据有效时，准入结果才可能通过 `coverage_basis: quiet-edges/v1` 解释原始短覆盖，并保留 `full_span_coverage_short: true`。派生的 `edge_coverage.coverage` 或成功标志本身不能作为通过依据。证据缺失、损坏、时长不匹配或实际窗口仍覆盖不足时继续需要复核。全文件均低于阈值记录为 `no_signal_above_threshold`，不会据此认定静音或成功。

真实样本的候选策略验证应在独立目录运行，只读原归档，保存每个 chunk 的诊断和文本一致性对照。上线和重新排队须在验证后另行执行；创建 PR 本身不更改生产策略。
