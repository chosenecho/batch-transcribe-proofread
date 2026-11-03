# ASR 隔离 profiling 入口

`scripts/profile_asr.py` 对固定的本地音频执行实际 `two_pass_transcribe`，完整保留强制对齐和质量诊断。
默认单次、最多 360 秒（显式上限不能超过 1800 秒），只读加载本地模型，不领取或写回业务任务。
默认保留 BF16、180 秒 chunk、第二遍 cache 关闭与预取关闭；显式实验参数单独记录在 execution policy。

已有硬件执行证据见 [2026-10-11 的 4090 验证记录](../evidence/asr-gpu-validation-2026-10-11.md)。
该次计数器权限不足、参考仅为历史 subtitle-ai，且短样本预取更慢；这些限制属于结果的一部分。

生产需先按原监督流程排空，确认没有 running 任务和 GPU owner，再由操作者维持独占窗口。
入口要求 `--ack-exclusive-device`，加载模型前通过 `nvidia-smi` 拒绝已有 compute owner。
该检查是瞬时观察，不是跨进程锁；`--nvidia-device` 必须和 `--device` / CUDA_VISIBLE_DEVICES 的映射一致。
完成或失败后恢复原生产角色，保留 failed attempts 与人工 hold。此入口不修改生产控制文件。

```bash
python scripts/profile_asr.py --mode baseline \
  --audio /srv/fixtures/fixed-24s.wav --warmup-audio /srv/fixtures/fixed-6s.wav \
  --model /srv/models/asr --aligner /srv/models/aligner \
  --output /srv/results/baseline-new --repeats 3 --ack-exclusive-device

python scripts/profile_asr.py --mode timeline \
  --audio /srv/fixtures/fixed-24s.wav --warmup-audio /srv/fixtures/fixed-6s.wav \
  --model /srv/models/asr --aligner /srv/models/aligner \
  --output /srv/results/timeline-new --ack-exclusive-device
```

baseline 不加载 profiler。timeline 使用 Torch CPU/CUDA profiler，保存可由 Perfetto/Chrome trace viewer
读取的 `timeline-0.json`，含 `asr.prepare`、`asr.decode`、`asr.align` 区间；不启用 stack/shape/memory tracing。
NVTX 模式给外部 Nsight 提供相同区间，可在确认权限与版本支持后有界采集：

```bash
ncu --nvtx --nvtx-include 'asr.decode/' --launch-count 20 \
  --metrics dram__bytes_read.sum.per_second,dram__bytes_write.sum.per_second,dram__throughput.avg.pct_of_peak_sustained_elapsed \
  --export /srv/results/decode-counters \
  python scripts/profile_asr.py --mode nvtx \
    --audio /srv/fixtures/fixed-24s.wav --model /srv/models/asr --aligner /srv/models/aligner \
    --output /srv/results/nvtx-new --ack-exclusive-device
```

计数器名/SM/Tensor 指标需针对本机架构先 `ncu --query-metrics` 核实，采集权限必须实际验证；
查询支持不等于采集成功。将 include 改为 `asr.align/` 可单独选取对齐范围。
`launch-count` 限制热点样本，不能以这些 kernel 代表完整任务；必要时再用 Nsight Systems 时间线定位。

`measurement.json` 保存输入 SHA256、实际策略/环境、单次 wall、完整输出哈希、质量风险、
PyTorch allocated/reserved 峰值；不导出字幕正文。加载和显式预热耗时分列。
计时只在整次请求前后 synchronize，原逐块 trace 继续表示嵌套 wall 时间。
只有 baseline 的 `normal_audio_s_per_wall_s` 有数值；profiler 会重放/插桩，带 profiler 耗时禁止用于正常吞吐比较。
allocator 峰值不含驱动/context，也不是总 RAM/VRAM。输出哈希一致仅说明重放一致，不能替代参考 CER/听校。
可用 `--reference-text /srv/fixtures/reference.txt` 提供独立 UTF-8 人工参考（最多 64 KiB），
报告参考 SHA256 和字符编辑距离/CER，不输出正文。规范化仅 NFC 与删除空白，保留大小写、标点，
比较工作量超过 4,000,000 个字符对时标记未测量；CER 不自动认可语义或时间戳质量。
`--model-dtype float16 --aligner-dtype float16` 可分别控制两模型精度，其默认均为 bfloat16。
该 NVIDIA 专用入口的结果不能充当 BW1000 海光实机验收。
重复相同音频的每次测量是独立基准样本，不将它们作为多个唯一归档音频来计算生产吞吐。

此入口提供复现工具，#292 仍须保存实际 trace/计数器与同语料正常基线，才能判断带宽、SM/Tensor、
CPU 准备或同步/启动瓶颈。`nvidia-smi utilization.memory` 是读写活动时间比例，不能折算 GB/s。
