# 专用 worker、可终止 ASR 会话与任务内准备

实现范围：#279 和 #288。工作流任务、依赖、attempt 和租约仍以 SQLite 为准；推理子进程仅接收音频与当前请求身份，返回结果，不接触数据库。性能收益仍需同配置真实 GPU 基准验证，离线测试只证明协议、进程故障边界和输出一致性。

## 任务类型与排空

`workflow run --role asr|acquisition|cpu|editorial` 选择固定任务集合；也可重复 `--kind` 精确选择。`asr` 只领取 ASR，`acquisition` 领取字幕和音频，`cpu` 领取字幕、音频、归档发布，`editorial` 领取校对和文稿渲染。`--only-editorial` 保留兼容，但不能与 role/kind 混用。未注册的 `index` 在首次 claim 前拒绝。默认通用运行只领取当前注册的 handler，避免执行没有实现的 INDEX。

默认运行至队列空闲后退出；`--poll-interval 5` 保持当前进程和 GPU 会话，持续查询新就绪任务，不为每次空闲重建模型。`--limit` 是整个调用的任务数量上限。

`--drain-file PATH` 检测到文件存在后停止下一次 claim，SIGINT/SIGTERM 同样请求排空。`workflow run` 和 `supervise` 的 `--drain-timeout SECONDS` 默认均为 60 秒，从首次观察到排空请求开始计时，期间当前任务继续 heartbeat。期限内完成的任务正常提交；合作 checkpoint 到期终止推理会话，并按原 attempt/owner 记录 `worker_drain_timeout`、清空租约。校对在每块请求前后同样检查。重复停止信号会立即中断仍在运行的 handler，使用相同失败码；一旦进入超时清理，不再用重复信号打断回收。

POSIX 主线程运行且调用方没有占用 SIGALRM/timer 时，独立 watchdog 在期限到达后也能中断阻塞的 Python/HTTP handler；结束时恢复原信号处理器。Windows、非主线程运行，以及调用方已有 alarm 的场景仅提供合作检查。Python 信号不能保证中断长期不返回解释器的原生代码，因此监督器仍在业务宽限后额外预留 5 秒清理时间，最终硬杀自己拥有的残存进程。严重数据库写竞争也可能耗尽此预算；未提交任务按原租约机制恢复，不按 worker 名批量重置任务。

心跳打开连接和续期均允许瞬时 SQLite/I/O 错误恢复，连接的锁等待保留较低的原配置、上限为 1000ms。恢复预算是当前租约时长的一半，成功续期后重置；持续失败或确定的契约错误向 executor/checkpoint 暴露 `lease_renewal_failed`，禁止接受 handler 的后续成功返回。合作 handler 会提前停止；无合作检查的阻塞调用仍需其超时或停机边界。租约已经丢失时继续使用原 fence，不能覆盖后来 attempt 的结果。

```bash
bili-asr workflow run --archive-root /srv/archive --role asr \
  --worker-id gpu-0 --poll-interval 5 --drain-file /run/bili-asr.drain \
  --drain-timeout 60 --gpu-session persistent
```

应用运行退出时恢复原信号处理器、关闭模型/会话和归档 session。排空文件由操作员创建/移除；应用不删除用户文件。

## 会话协议与资源归属

`asr.session.AsrInferenceSession` 每路只允许一个在途请求。请求与响应绑定 protocol v1、generation、随机 request ID、job ID、worker owner、attempt_count、冻结 profile digest 和实际 runtime binding 身份。完整 ASRConfig 或 binding 身份变化时，先销毁旧进程，再启动新 generation。迟到或身份不匹配的响应会拒绝，并销毁会话，不会被下一条任务使用。

每次请求执行原 `two_pass_transcribe` 热词治理；characters、segments、coverage、language 和 diagnostics 按任务/遍清理。会话可复用模型；结果与热词证据不跨音频复用。CPU runner 的既有复用保持，实际 checkpoint binding 改变也重建 CPU runner。`--runtime-bindings` 使用经文件 manifest 与冻结 revision 检查的模型重定位，不改写历史 profile；脱敏 binding 身份写入 ASR evidence 和 session key。

父进程约每 50 ms 校验当前租约、取消状态与排空期限，独立 heartbeat 继续续期。取消、失权、deadline、模型异常、崩溃、协议异常均销毁会话，下次任务重建；取消以权威数据库状态计数。最终 transcript 与出版请求继续使用现有 owned transaction/fence。

推理响应在读取前、读取并完成租约/取消检查后均校验期限。`audio-scaled` 策略同时检查整段总预算、首次有效进展前的启动期限，以及后续无进展期限；达到期限时，即使收到当前请求的进度帧或成功结果，也按 `total_deadline`、`startup_deadline` 或 `no_progress` 拒绝并销毁会话。迟到的进度不能重置已经到期的 watchdog；期限内收到的有效进度仍可续展无进展计时，整段总预算保持有限上界。

大结果在 child 存活时持续读取，读取完成后再 join，避免 IPC feeder 与父进程 join 相互等待。队列各深度为 1，不承担业务任务调度。`--gpu-session oneshot` 使用同一可取消协议，每个任务完成后关闭模型进程，可用于回退对比。公开旧一次性 transcriber 仍兼容历史注入端口。

本地 WSL/Linux 下推理会话建立独立进程组，包含其 ffmpeg 子进程。Linux 设置内核 parent-death SIGKILL，使持有 GIL 的推理也不能在父进程退出后继续占用 GPU；独立的轻量 EOF guardian 在 leader 退出后清理进程组中的解码子进程。普通 parent guardian 继续用于跨平台检测。正常 close、超时与取消也回收 child。WSL 测试覆盖父进程 SIGKILL、模型故意持有 GIL 及真实 decoder 后代的终止。Windows 使用 multiprocessing handle 的终止和 parent guardian；POSIX 进程组及 SIGKILL 验收不适用于 Windows，不能据此宣称 Windows 子进程树验收已完成。

## 单机固定槽位监督

`workflow supervise` 直接拥有控制进程 handles。它在同一归档取得独占监督锁后创建固定数量角色槽位，监视自己的控制进程，不扫描全机 argv/PID，不把 multiprocessing 推理进程计入控制槽位。一个槽位必须先退出、join、close 后才能替换，防止补成第三路 GPU。锁不能限制操作员在外部手工启动额外 worker；迁移时应先排空外部旧 worker。

```bash
bili-asr workflow supervise --archive-root /srv/archive \
  --asr-slots 2 --cpu-slots 1 --editorial-slots 2 \
  --poll-interval 5 --drain-file /run/bili-asr.drain --drain-timeout 60
```

重启采用 0.5 秒起、最多 30 秒的指数退避。连续快速退出超过 `--max-restarts`（默认 8）时监督器排空并退出；健康运行 60 秒后重置该槽位连续错误计数。各角色 0–32 个槽位，可按设备/外部服务预算选择；默认不硬编码生产“两路”。同归档第二个 supervisor 在创建任何进程前拒绝。

监督器收到信号或排空 token 时停止补槽，对自己拥有的控制进程请求排空，业务期限加 5 秒清理余量后终止剩余进程。控制进程若发现监督父进程异常退出，会请求排空并使用相同余量后硬退出；其推理 child 由自己的 parent guardian 回收。失败和租约恢复仍属于原 workflow，不另造恢复队列。

## 任务内 trace 与实验预取

每遍 diagnostics 包含相对本进程 `perf_counter` 的阶段 trace：模型加载、音频准备、切块、CPU processor 输入、device transfer、generate、decode 后处理、输入等待、逐块 decode/align。事件明确标注 `measurement=wall`。它不是 GPU kernel 时间，没有每块强制同步；不同进程或不同遍的相对起点不能直接拼接为一条 GPU 时间轴。session evidence 提供 parent wall、任务间 gap、generation、重建原因与复用状态。

同一双遍任务可复用解码/重采样后的 waveform，身份含源文件流式 SHA256、目标采样率和分块策略；准备前后及两遍之间再次确认内容身份。源字节变化会拒绝当前任务。缓存随该双遍作用域退出清理，失败同样清理，不跨任务保留波形。每遍独立 generation、prompt、coverage 和诊断保持原规则；没有有效热词仍跳过第二遍。

`--asr-prefetch` 默认关闭，开启后只预备下一块 CPU processor 输入，深度固定为 1，当前 GPU decode/align 仍串行。准备线程使用单独复制的 processor，避免与主线程 decode/tokenizer 操作共享可变状态。无法复制时退回串行。`--asr-prefetch-bytes` 默认 64 MiB；按 waveform ×64 做保守输入预留，并检查返回 CPU tensors 的实测字节，超过预算的输入不保留为预取结果，转串行。诊断记录 decoded waveform bytes、prepared input 峰值、提交数、等待与退回原因。

同一任务的两遍转写可复用该独立 processor：第一遍线程池完全退出后，第二遍才重新创建准备线程并使用 clone；每块输入及第二遍热词 prompt 仍重新构建。复用仅存在于 `two_pass_transcribe` 的任务作用域，成功、异常和 release 均清理保留引用，后续任务重新克隆。普通单遍调用按原生命周期执行；诊断 `processor_reused` 标明本遍是否命中任务内复用，命中时 `processor_clone_attempts=0`、`processor_clone_s=0`。

这个预算约束额外准备的输入，不是整个进程 RSS 的硬上限。模型、完整音频、processor clone、第三方 processor 暂态分配和 RF64 解码临时文件仍须纳入真实基准资源测量；特别长音频或自定义 processor 应保持串行。没有预读未 claim 的下一条任务，跨任务 ownership 不被绕过。

### 准入与对齐诊断（#295 / #299）

预取报告的 `schema_version=2` 保留旧字段，并增加逐块 `chunks`、`consumed`、`discarded` 和 `fallback_counts`。每个候选下一块记录实际 float32 波形字节、包含短尾 padding 的预留、观测输入字节、状态、等待与回退；首块始终串行，未开启预取时不运行 processor 或复制/填充下一块。`fallback` 保留最后一个回退原因以兼容旧消费者，累计统计应使用 `fallback_counts`。

估算策略仍为 `waveform_x64_v1`，未调高默认预算。16 kHz 单声道 180 秒 float32 块是 11,520,000 bytes，预留为 737,280,000 bytes（703.125 MiB）；64 MiB 默认预算会拒绝该候选并串行准备，短尾块可能通过。开关开启不等于所有块实现重叠。处理器输入大小不能确定时记录 `prepared_input_size_unknown` 并回退；返回输入实测超限记录 `prepared_input_budget`，释放预取引用后按原串行路径重建。实测发生在分配之后，不能限制第三方处理器暂态峰值。

processor 副本与线程池在第一个通过预算准入的下一块出现时才创建。只有一个块、所有下一块超预算或关闭预取时不会复制 processor。每遍最多尝试一次复制，失败后可准入候选仍以 `processor_not_cloneable` 回退，超预算候选保持 `input_budget` 原因；清理同时释放未复制的原 processor 引用。报告新增 `processor_clone_attempts` 和 `processor_clone_s`，后者是包含副本/线程池初始化的 wall 时间。副本仍限定于该遍，不跨任务复用。

准备线程只返回输入和计时元数据，主线程负责合并诊断；异常退出仍取消未开始的准备、等候已运行的准备并清理资源。硬取消/超时仍由可终止 session 边界保障；线程等待自身不提供新的硬期限。

对齐新增 `align_prepare`、`align_transfer`、`align_forward`、`align_postprocess`，均带 chunk index，沿用 `measurement=wall`。父事件 `align` 仍保留，不将父子事件相加充当 GPU 时间，也不引入每块 GPU synchronize。batch 和对齐编译仍未启用。

## 验证与实际 GPU 验收

本地 WSL 的 `test_asr_sessions.py` 使用真实 spawn 进程验证复用、任务隔离、配置/binding 重建、异常/超时/硬崩溃、取消、迟到身份拒绝、大 IPC 结果与父 SIGKILL。`test_asr_prefetch.py` 验证两遍只解码一次、源变化拒绝、准备与 decode 的实际线程重叠、与串行相同 cues/coverage、预算与 clone 回退。`test_workflow_workers.py` 验证 claim 筛选、排空、期限、固定槽位/退避/互斥和真实空闲控制进程退出。没有修改旧测试收集忽略规则。

真实 GPU 关闭 issue 前，使用固定短/长/多块音频、单遍/双遍和相同 profile、并发数，对 persistent/oneshot、serial/prefetch 分别测完成音频秒/墙钟、P50/P95、失败率、coverage、人审差异、CPU/RAM/VRAM 峰值、RF64 临时盘和输入等待。保留 trace 与 frozen config/binding 身份。离线一致性不证明模型真实线程安全或吞吐改善，GPU 利用率也不能替代这些指标。
