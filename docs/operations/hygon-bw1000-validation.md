# 海光 BW1000 兼容支持与验收流程

对应 #289。2026-10-10：提供精度配置、海光探测、受监督准备和部署检查。
**尚无 BW1000 实机验收证据，工程回归不表示已验证硬件支持。**
范围仅 BW1000，不把 K100/Z100SM 结果代入。

## 环境门槛

项目要求 Python ≥3.12、Transformers ≥5.13。[官方 FlagTree 海光说明](https://docs.flagos.io/projects/FlagTree/en/latest/getting_started/multi-backend-prebuilt-docker-image-install/install-hcu.html)
的 python3.10 示例不能直接当作项目环境。

记录目标完整型号/卡数/显存、OS/CPU、驱动/固件/DTK、厂商 Torch、Python、
Transformers/Accelerate 和音频工具。隔离环境需满足项目要求，应用依赖不得覆盖厂商
Torch。保存 wheels/容器来源、版本、SHA-256，不覆盖原 AMD/CUDA 环境。
当前无已验证厂商版本组合，本文不提供未经验证的可用镜像/安装承诺。
版本无法同时满足时记录冲突/最小错误，不直接降低 Python 要求，不凭 cuda 名称宣称支持。

## 双模型精度

两个模型保留 BF16 默认，各自可选 float16：

```bash
export BILI_ASR_MODEL_DTYPE=float16
export BILI_ASR_ALIGNER_DTYPE=bfloat16
bili-asr check-asr-env --backend hcu
bili-asr check-asr-env --backend hcu --probe-gpu
bili-asr workflow plan --archive-root /srv/bw1000-validation --part-id 1 \
  --model-dtype float16 --aligner-dtype bfloat16 --profile-key bw1000-fp16
```

精度进入冻结 profile/session key/加载/provenance，输入随各自实际 dtype 转换。
未知精度拒绝，无静默回退。默认 profile 旧 v2 JSON/digest 逐字节保持；非默认精度以
版本化 precision 扩展产生新身份。执行冻结 profile 不重读临时精度环境变量。

海光探测独立于 AMD/WSL；没有海光命名设备拒绝 hcu。真实张量运算仅证明小算子，
不能证明所有模型算子。FP16/BF16 都为每参数 2 字节，不预设显存/速度收益。

## 实机验收

先独立张量探测，再分别选择精度组合。外层 deadline 覆盖加载和真实推理：

```bash
timeout 900 python scripts/verify_gpu.py --backend hcu --device cuda:0 \
  --model-dtype float16 --aligner-dtype bfloat16 \
  --audio /srv/fixtures/chinese-short.wav \
  --model /srv/models/Qwen3-ASR-1.7B-hf \
  --aligner /srv/models/Qwen3-ForcedAligner-0.6B-hf \
  > bw1000-short-fp16-bf16.json
```

输出实际精度运算、模型结果、音频 SHA-256、provenance、coverage 和诊断。
要求 cues 与 character alignment，不能以只有正文通过。各精度同样本/参考比较，
失败保存具体算子/版本的受控记录，不暗中切换方案。

随后在独立归档和验证过的 binding 上经真正 workflow 入口验收：

1. 短中文、长多块、M4A/AAC、语言与实际热词双遍。
2. 冷启动/预热/稳态连续任务，设备/dtype/revision 可追溯。
3. 超时、取消、owned child 回收、准备失败不消耗 attempt、失败后显式恢复。
4. 写回、v3 六成员 bundle 与完成 marker、导出读取，时间戳范围/顺序和 coverage。
5. 人工参考 CER、漏句/重复/异常；有来源时间戳才计算对齐误差。
6. 实测峰值显存、阶段 wall time、RTF、成功/失败，注明是否包含加载。

连续任务用原始完整音频秒/共同墙钟；无同条件基线只报告绝对结果。
不开展 perf profiling、新引擎或量化实验。

关闭 #289 需可复现厂商环境、至少一种精度完成双模型全流程、短/长/M4A/连续任务结果，
以及质量/稳定性证据与限制。当前测试验证身份、加载参数、分派和进程/队列行为；
目标环境、真实模型算子和 GPU 释放仍待实机。普通工作站缺 GPU 不代表海光硬件失败。
