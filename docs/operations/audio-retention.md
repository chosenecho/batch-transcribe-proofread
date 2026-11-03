# 独立音频自动归档

音频维护是独立的 `artifacts policy-run --watch` 进程，既不运行 ASR，也不修改任务状态、模型配置或历史 attempt。新策略采用 `artifact-policy-v2`，固定只选择音频。已有 `artifact-policy-v1` 和 stored-only `artifact-package-v1` 保持原有契约；新音频包采用 `artifact-package-v2`，其原始对象身份、压缩配置、manifest 摘要与容器摘要分别保留。新读取器支持两种包的逐对象恢复。

## 明确配置与授权

先对停止的归档执行已有的显式 `artifact-online-v1` 升级流程（见 [artifact-storage.md](artifact-storage.md)）。没有该扩展时，命令报错，不隐式安装数据库扩展。没有保存的策略默认为 `off`。

```sh
bili-asr artifacts policy-show --archive-root /srv/bili-asr --policy-id audio --default-version 2 > /etc/bili-asr/audio-policy.json
```

仓库的 `deployment/audio-retention-policy.example.json` 是完整的默认配置。修改并保存配置前应检查以下字段：

| 配置 | 执行语义 |
| --- | --- |
| `mode` | `off` 不执行；`copy` 只备份；`offload` 请求有条件迁出 |
| `allow_delete` | 默认 `false`。只有 `mode=offload` 且 `allow_delete=true` 才授权自动删除本地音频，配置摘要和授权随每次运行入库 |
| `target_id` | 主备份目标的逻辑身份，必须事先绑定目录；机器路径通过命令行传入 |
| `replica_target_ids` | 额外目标身份列表，每批先完成所有配置目标的备份 |
| `minimum_verified_copies` | 最终删除前至少需要的已核验目录副本数，必须大于零且不能大于目标数；目录 inode/文件标识相同不重复计数 |
| `minimum_age_seconds` | 增加副本只按稳定源文件 mtime 计龄；删除还按源文件 mtime 与所有直接、间接消费者最后更新时间的最大值计龄 |
| `local_retention_seconds` | 完成年龄达到此期限时允许迁出。必须不小于 minimum age |
| `maximum_local_audio_bytes` | 所有根目录中音频文件按物理文件去重的容量上限；超过时可迁出已满足 minimum age 的批次 |
| `trigger_free_bytes`、`stop_free_bytes` | 低空闲空间触发，较高水位停止；冻结批次的容量/水位决策允许一次有界超额迁出 |
| `batch_bytes`、`batch_objects` | 每轮最多唯一载荷字节与对象数。大于批次上限的单音频不自动拆分，运行报告显示没有可选对象；应根据最长视频配置上限 |
| `compression_level` | lossless ZIP DEFLATE 0–9，默认 6；不转码音频 |
| `bytes_per_second` | 扫描、写包读取和副本复验统一计量和限速，0 显式不限速 |
| `scan_interval_seconds`、`backoff_seconds` | 成功后的扫描间隔与失败后的退避；持久化期限，重启继续遵守 |

v2 每个策略只有一个执行 lane。同目录目标的音频维护策略通过内核锁串行；各对象沿用共享 reader / 独占迁移 fence。副本数是不同目录的已验证副本数；同一磁盘上的两个目录不能保证独立磁盘故障恢复，需要操作者将额外目录布置到不同介质。

准备并绑定目录，再先使用 `mode=copy` 验证备份和恢复：

```sh
bili-asr artifacts bind-target --archive-root /srv/bili-asr --target-id cold-audio --target-root /mnt/audio-backup
bili-asr artifacts bind-target --archive-root /srv/bili-asr --target-id second-audio --target-root /mnt/second-backup
bili-asr artifacts policy-set --archive-root /srv/bili-asr --policy-id audio --config /etc/bili-asr/audio-policy.json
bili-asr artifacts policy-run --archive-root /srv/bili-asr --policy-id audio --target-root /mnt/audio-backup --storage-target second-audio=/mnt/second-backup --holds-file /etc/bili-asr/audio-holds.json
```

`audio-holds.json` 使用现有 `{"version":1,"holds":{...}}` 契约。它是本次运行读取的外部 hold 观察，未提供时无法确认 hold 状态，因此禁止删除；增加副本仍可继续。只有实际检查外部控制状态后才可改用 `--no-external-holds`。在策略文件中明确修改 `allow_delete=true` 与 `mode=offload` 后执行 `policy-set`，即授权符合已保存条件的自动删除；不逐文件询问。任何过程中配置变更都会在下一个检查点停止旧批次。

## 独立常驻与停止

```sh
bili-asr artifacts policy-run --archive-root /srv/bili-asr --policy-id audio --target-root /mnt/audio-backup --storage-target second-audio=/mnt/second-backup --holds-file /etc/bili-asr/audio-holds.json --watch --drain-file /run/bili-asr/audio-retention.stop
```

可按本机路径调整 `deployment/audio-retention.service.example` 后安装为独立 systemd 服务。示例默认只绑定主目标，配置额外目标时必须向 ExecStart 添加对应 `--storage-target`。停止文件在每轮、每个最多 1 MiB 的流式读取及限速等待中复验；限速等待最多 0.25 秒一个检查点。SIGINT / Ctrl-C 同样产生持久化失败收据并退出 watch。再次启动前显式移除停止文件。正常流程清理自身 target-side staging；它不会扫描、删除未知孤儿文件。

所有 unfinished、failed、cancelled 任务和 running attempt，以及通过依赖链关联音频的 AI 修复/发布任务、等待恢复的任务、人工 pin 和外部 hold 都阻止迁出。运行中的实际 ASR 输入另有对象 fence、消费者 pin 和独立私有文件。私有输入、未知文件、未绑定历史文件不进入候选，维护不会解除人工 pin/hold。

增加副本与删除资格分别冻结：`backup` 使用新的 `artifact-copy-plan-v1`，
`release` 仍使用 `artifact-offload-plan-v1`。AI queued/failed、旧 ASR failed、
cancelled job、活跃 reader pin 和 retention hold 不阻止复制已验证的稳定对象；
它们继续阻止删除。复制取得共享对象 fence，可与读取并存，但不允许源变更；
摘要、代次、完整组、已登记身份、目标空间检查和复制后目标复验继续强制执行。
copy plan 在 transfer 的 offload 模式及 release reconciliation 中会被拒绝，不能用
复制资格冒充删除授权。独立预览可使用：

```sh
bili-asr artifacts plan --archive-root /srv/bili-asr --purpose copy --kind audio --target-id cold-audio --out /tmp/audio-copy-plan.json
```

收据中的 `copy_selection` 表示本次有界批次，`copy_backlog` 表示该批次尚未全部
完成复验的副本工作，`scope=selected_batch` 不代表全归档扫描的累计待办。
成功获得全部目标收据后该批次 backlog 归零；`release_blockers` 单独保留删除阻碍。
因此 `state=blocked` 可以同时有成功备份收据和零删除字节，并不表示备份失败。

有界批次优先选择尚未取得全部配置目标副本收据的对象，再按策略中的大小或时间
排序。这样，已成功复制但因下游任务未完成而留在本地的音频，不会每轮占满批次，
让其他音频始终轮不到备份。目录副本收据只影响入选顺序，不替代目标当前可达性、
摘要复验或删除保护；全部目标都有收据后，已备份对象仍可入选重新校验或释放。

## 压缩、峰值空间和审计

源音频使用 1 MiB 缓冲流式压缩，恢复也限制每次解压读取和输出大小，且按原始 SHA-256 与大小验证。manifest 保持 uncompressed 并有 16 MiB 上限，ZIP central directory 有 64 MiB 上限。每批先 fsync 写包并从目标重新读取整个容器和所有原始对象，再登记副本。最终删除前复验原始目标对象、所需副本的容器摘要/manifest/原始摘要，以及目标目录实例和文件 generation；最后短数据库写事务同时检查最新队列、pin、hold 和副本 generation。

M4A/AAC 本来已压缩，DEFLATE 不承诺省空间。每轮收据的 `compression` 保存 original payload、compressed payload、container bytes、实际 container/original ratio；该比例可以大于 1。已经封装的、当前重新核验合格的 v2 包可复用，copy 后 offload 不再生成一份重复载荷包。

新增 target-side 空间按未存在载荷原始总字节，加 1% 的 DEFLATE 膨胀余量和 `max(64 MiB, object_count × 4096)` 保守检查，压缩仍可能膨胀。只暂存一个新包，所有源字节保持原地直到验证完成；目标空间不足、离线或损坏时不会删除唯一源。`target_temporary_space_upper_bound_bytes` 显示单批保守目标空间预算，`local_audio_bytes_before/after`、`released_bytes`、`scan` 与 `io` 显示实际空间与读取成本。该预算不是文件系统硬配额；另一个归档或绕过协作锁的进程仍可能占用空间，真实写入失败继续保留源。

ASR 另有私有输入，Linux 优先 CoW、其他平台有界流式复制，预留完整输入大小加 64 MiB 余量；解码/对齐工作区还使用 online 共享容量预留。实际峰值需要加上活跃输入与工作区，不能只用已下载音频大小估计。长时间不能释放时通过下载 admission control 阻止新增大文件，不宣称无限输入下仍能自动保证硬配额。

`artifact_policy_runs` 保存完整 policy、policy hash、backup/release 冻结计划、结果、已释放字节、压缩比例和重试期限。包、副本、transfer、release intent 继续使用既有 journal。业务表和模型配置不因维护而改变。音频策略只处理音频；所有任务状态的独立备份走 task-state/reference/maintenance 备份流程，本策略不会压缩或清理这些备份。

## 失败和恢复

可控中断若已隔离源，会仅按冻结计划和已持久化 intent 复验该隔离文件，再恢复原本地路径，不扫描未知文件。仍有活动 reader、源发生变化或数据库不可写时保留隔离字节，收据记录 `recovery_pending`，待问题解决后再次运行同一策略；不强制解除 fence。

进程被强杀后 kernel fence 自动释放，隔离字节和收据仍保留。重启相同配置的维护服务会读取同一冻结 backup/release plan，重新核验目标，再恢复/继续释放；新的消费者/hold 可以阻止释放并恢复本地源。配置暂时停用不删除隔离内容；重新启用完全相同配置可定位原配置摘要下的未完成收据。无法核验的源与未知 target staging 应按既有 artifact-storage 的显式停止、核查和恢复步骤处理。

升级前遗留的未完成冻结计划若仍把 backup 标成 offload-plan，会先按既有
release journal 恢复其确切隔离源，再从当前事实重新规划 copy/release；旧计划摘要
记入 `legacy_plan_replanned`。不会清理历史 attempt 或把旧的删除意图解释为 copy。

workflow 使用下面的绑定，在 claim 前按需恢复正确对象，并保持原 audio attempt / ASR profile 证据：

```sh
bili-asr workflow run --archive-root /srv/bili-asr --storage-target cold-audio=/mnt/audio-backup --storage-target second-audio=/mnt/second-backup
bili-asr artifacts restore --archive-root /srv/bili-asr --target-id cold-audio --target-root /mnt/audio-backup --object-id ORIGINAL_SHA256 --storage-key audio/ORIGINAL_NAME.m4a
```

恢复与 ASR 后续验证使用原音频字节身份；新 ASR profile 只产生它自己的新 attempt。目标失效或恢复空间不足时等待恢复，不能重新下载另一代输入冒充原对象。
