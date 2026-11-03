# ASR 正常运行的只读吞吐汇总

从已有 SQLite evidence 与 workflow attempts 生成有明确窗口和缺失值的 JSON 报告。它不运行模型，不执行 retry/claim，不变更 hold，也不加载 profiler。

```bash
bili-asr workflow asr-performance --archive-root /srv/archive \
  --start 1791504000 --end 1791507600 --max-attempts 10000
```

`--start`/`--end` 是 UTC epoch 整数秒，指定 `[start,end)` 的共同墙钟窗口。输入要求 `0 <= start < end`；attempt 限额为 1–100000，默认 10000。候选超过限额时拒绝整份报告，提示缩小窗口或提高限额，不悄悄输出截断结果。报告各查询在同一 read transaction 快照完成；调用者已有事务时不提交或回滚其事务。以 READ 模式打开归档；本次静止测试归档的 DB/sidecar 字节校验保持不变，并发 writer 自身写入不在该校验范围内。

## 统计口径

| 字段 | 定义 |
| --- | --- |
| `unique_success_audio_s` | 完整落在窗口内的成功 workflow ASR attempt，以 evidence 中已有音频 SHA256 去重后的原始音频秒。相同输入重复执行或双遍不会重复计音频。 |
| `audio_s_per_wall_s` | 上述原始音频秒 / 窗口共同墙钟秒。成功输入身份/时长缺失时为 `null`，而非假设音频为零。 |
| `known_audio_s_per_wall_s_lower_bound` | 已知输入的窗口吞吐下界，可结合 unknown 数量查看。跨边界成功明确排除在该采样口径外。 |
| `audio_credit_complete` | 完整在窗内的成功 attempt 均有可靠音频身份/时长时为 true；缺失或冲突时为 false，完整吞吐 `audio_s_per_wall_s` 为 null。 |
| `unknown_success_audio_attempts` | 完整在窗内成功但缺少可靠音频 credit 的 attempt 数，包含所有涉及冲突身份的成功 attempt。 |
| `conflicting_audio_identity_count` | 完整在窗内的成功 evidence 对同一 SHA256 声明不同时长的唯一身份数；组内统计参与该报告窗口冲突的身份。 |
| `conflicting_success_audio_attempts` | 涉及上述冲突身份的完整在窗内成功 attempt 数，是 unknown 的子集，不能与 unknown 相加。 |
| `overlapping_attempt_wall_s` | 所有与窗口重叠的 attempt 耗时截入窗口后求和；并行时可能大于窗口，**不是吞吐分母**。 |
| `failure_cancelled_attempt_wall_s` | 失败/取消 attempt 在窗口内的耗时。 |
| `retry_attempt_wall_s` | retry attempt 在窗口内的耗时，可能与失败耗时重叠，二者不相加当总成本。 |
| `retry_attempts` | 同 job 已有更早开始的 attempt；同秒以插入顺序消歧。更早 attempt 可在窗口外。 |
| `terminal_attempt_success_rate` | 重叠样本中成功 /（成功+失败+取消）。它是 attempt 成功率，不是唯一任务成功率。 |
| `unique_asr_job_status_at_snapshot` | 有重叠 attempt 的唯一 ASR job，按报告快照中的 queued/running/succeeded/failed/cancelled 分类；同 job 多次重试只计一次。 |
| `terminal_asr_job_success_rate_at_snapshot` | 上述唯一 ASR job 中 succeeded /（succeeded+failed+cancelled）；queued/running 不算终态，也不等于整个发布链路成功率。 |
| `successful_attempt_latency_s` | 完整在窗内成功 attempt 的耗时 P50/P95（线性插值），附样本数。历史时间仅精确到秒，零秒样本照实保留。 |
| `excluded_boundary_success_attempts` | 开始在窗口前或结束在窗口后、无法完整归因音频的成功 attempt 数。不会按耗时比例猜测处理音频量。 |
| `pass_counts` | 完整在窗内成功 evidence 的单遍、多遍和未知数量。 |
| `prefetch_observed_totals` | 同一成功 evidence 范围的 submitted/consumed/discarded/input_wait_s；缺失字段不造零。 |
| `prefetch_fallback_counts` | v2 记录的已知回退计数；旧 v1 只记录最后原因，不能重构逐块次数，另计 legacy passes。 |
| `acquisition_run_outcomes` | 所有重叠 ASR acquisition runs 的 outcome 次数，包括没有 workflow attempt 的独立历史运行；不与 workflow 计数相加。 |

开始在 end 的任务不在窗口，结束恰好在 end 的完整任务可计 credit；结束在 start 的非零耗时任务不重叠。outcome 是报告快照中的存储终态，不能还原“窗口结束时它是否仍 running”；报告明确声明该限制。

## 配置、脱敏和未知值

报告按冻结 profile SHA256、runtime binding 身份 SHA256 和已记录 execution policy SHA256 分组。整个报告的音频去重独立于分组；跨配置同输入会在各自组内计一次，**不能相加各组音频量当总量**。每组仍用共同窗口分母，表示该组对窗口的贡献，不是该配置独占设备的 benchmark。

缺失 binding/有效 policy 为 `unknown`，不会根据另一成功 attempt 猜测失败的配置。缺少有效策略时分组不能用作后端排名。旧 evidence 能读取，未知 schema、缺失、无效结构和不完整音频身份显式计数。完整在窗内的成功 evidence 对同一 SHA256 声明冲突时长时，该身份在总体及所有参与组中的音频 credit 全部撤回；后续相同时长的记录不会恢复 credit，结果不依赖读取顺序。冲突计数显式保留，其他可靠身份、成败、耗时和预取指标继续报告；不选择第一条或最大时长，也不改写历史证据。冲突域统一为报告窗口的完整成功样本，跨边界或失败 attempt 不参与时长比较。仅当前 audio 表与历史 evidence 的时长不同不会产生该冲突。

`evidence_counts` 的诊断计数不互斥：缺失 evidence 同时无法按支持的 schema 读取，会计入 `missing` 和 `invalid_or_unsupported`，两者不能相加作为缺证据 attempt 总数。

查询仅投影性能所需字段，不复制逐块字幕/正文进聚合器。输出身份为摘要，错误正文、Cookie、API key 和任意回退文本不写入报告。聚合器不重新读取或哈希大音频文件，复用已有 SHA256。记录读取受 attempt 数量限制，但 SQLite 仍需扫描相关历史，单份旧 JSON 的解析成本与其大小有关，不能称全链路常量内存或固定响应时间。

新运行在每遍记录版本化 `execution_policy`：请求/实际精度、实际 attention（未知保留 null）、checkpoint cache 默认、第二遍 cache、compile/Graph、chunk/batch 和预取预算。生效策略摘要同时包含实际安装包与可读取的 GPU 名称、容量、SM 数及架构，配置/设备变化不会混成同一组。CPU token 预算在输入迁移前按原 mask 公式计算并随准备结果携带；默认推理行为和历史 profile canonical JSON 不变。

每遍拥有独立的 `clock.domain_id`、PID、perf_counter 起点与 UTC 锚点/采样误差；session 记录父进程锚点、request/job/attempt/generation 和配置 key。时间轴可以按锚点估计关联，不能把不同进程/遍的相对时间直接相加，也不能称为同步的 GPU kernel 时钟。正常路径没有新增模型请求或逐块 synchronize。

driver、源码 commit、CPU RSS/GPU allocator/采样峰值、失败阶段资源和最终发布吞吐仍明确为 unknown；资源采样当前 disabled。`asr-performance` 不证明质量或定位带宽/SM 瓶颈。历史证据缺失的策略/硬件不能反向补造，#292 的硬件 profiling 仍独立验收。

## 本地验证

回归覆盖重叠执行、重复源音频、双遍、失败/重试、跨窗口、未知/未来 evidence、限额拒绝、旧预取报告和敏感字段；并发 WAL writer 下验证单一快照，并验证不影响调用者事务。真实轻量 workflow 执行 fake ASR 后落库，再由真实 CLI 读取报告，静止 DB 字节保持不变。缺库 READ 拒绝且不创建根目录/数据库。

接口回归不能证明真实性能收益；硬件测量与限制见 [4090 验证](../evidence/asr-gpu-validation-2026-10-11.md)。
