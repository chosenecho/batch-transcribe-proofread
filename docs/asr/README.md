# ASR 配置与执行

[返回文档入口](../README.md)。先确认 [部署就绪](../operations/asr-deployment-readiness.md)，再选择配置、worker 和有界优化。参数和策略会随任务冻结；历史测量不能直接推导当前机器的收益。

| 文档 | 使用场景 |
| --- | --- |
| [配置与诊断](asr-configuration.md) | 模型版本、配置快照、逐次 evidence 与质量标记 |
| [Worker 与持久会话](asr-workers.md) | 角色、固定 slot、drain、持久模型及准备预算 |
| [有界批处理](asr-batching.md) | 单任务 ASR/alignment batch、预算和串行回退 |
| [显式推理策略](asr-runtime-strategies.md) | precision、attention、cache、compile 与能力检查 |
| [对齐与覆盖策略](asr-output-policy.md) | 模型分数路径、近静音首尾证据与旧记录兼容 |
| [吞吐报告](asr-performance.md) | 只读统计窗口、重试成本、缺失值及预取证据 |
| [Profiling](asr-profiling.md) | 隔离 GPU 对照、trace 与测量口径 |

这些文档记录处理实现与配置；本次同步未重新进行 GPU 实测。实际验收范围见 [迁移说明](../extraction.md)。
