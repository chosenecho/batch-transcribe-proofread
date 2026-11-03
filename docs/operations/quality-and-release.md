# 运行健康、真实验收与恢复交接

## 日常只读检查

复用已有 CLI，不另建队列或自动重试器。在实际归档主机上指定现有档案和
实际控制文件，读取状态与最新失败；JSON 可交给现有监控采集：

```bash
bili-asr workflow status --archive-root /srv/archive --json --failed \
  --limit 100 --control-state /srv/production-ops/state.json
bili-asr retry-maintenance health --help
```

路径是示例，必须使用部署中的真实路径。没有外部控制文件时，仅在事实如此
时显式声明 `--no-control-state`，不能因为文件不可用而隐藏 holds。失败分页
不代表全库总数，状态报告中的 jobs/attempts 应分开解读。日志和导出报告不得
包含凭据、签名 URL、模型私有路径或字幕正文。

关注时间新鲜度：worker 在运行不代表重试维护健康，maintenance 成功不代表
业务任务在推进。按[重试维护手册](retry-maintenance.md)检查各自产生的 health
快照，阈值取部署实际轮询周期的数倍。记录观察时间、最旧 queued/running、
最近成功、错误分组、磁盘余量及实际 AI 消耗；连续无进展时调查依赖、hold、
租约和输入，不扩大 worker 或自动清除 hold。成本限额沿用实际部署配置。

## 有界实机 GPU 验收

仓库的默认 CI 使用模型替身；GitHub 无 GPU runner 时实机状态为待验证。
在现有 GPU 主机的独立验收档案与已准备好的模型环境中执行：

1. 记录完整源码 SHA、Python/Torch/驱动版本、实际 GPU、模型 binding 的
   revision/manifest 身份及时间。用现有 `check-asr-env --backend <cuda|rocm|hcu>`
   做只读检查，再显式加 `--probe-gpu --probe-timeout 20` 做有界设备探测。
   探测通过仅代表设备和精度运算通过，模型尚未执行。
2. 按[模型就绪与预热](asr-deployment-readiness.md)准备自有、短且带语音的
   sentinel，在隔离验收档案中显式预热识别和对齐。验收输入不使用未经许可
   的真实第三方音视频，不触碰生产 queued jobs。
3. 执行一个明确范围的验收任务，检查输出时间戳、文本、对齐、质量门禁、
   任务状态、attempt 和可恢复性，记录实际耗时及峰值资源。选择已有小样本
   避免不必要下载或 AI 费用；不能仅凭 cues 数量宣称识别质量合格。
4. 保留已通过、blocked、not_run 的独立状态。无模型文件、无硬件或无法
   证明输入/模型身份时停止该验收并保留原因，不安装未审查驱动、不改生产
   配置、不把低层设备探测升级为“ASR 已验收”。

## 备份恢复与回退

`Release validation / recovery` 使用合成档案验证程序恢复能力。生产备份的
有效性还需按[完整快照](archive-snapshots.md)在隔离路径恢复并运行 doctor/
check，对比数据库历史与 artifact 哈希。引用备份还须按
[引用备份手册](remote-reference-backups.md)显式读回外部对象；offline check
不证明外部包仍存在。保存与执行权分离，恢复不自动启动 worker 或重试任务。

发布记录至少包含：源码与构建 SHA、配置差异、原归档与备份身份、恢复目标、
实际演练结果、worker 停止/交接、holds 文件、版本兼容与负责人。破坏性升级
前先核验备份；回滚程序版本前确认它仍能读取新契约，不能直接降级写旧格式。
出现失败先保留日志、attempt 与源数据，再按照已验证路径处理。
