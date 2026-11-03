# 处理功能同步记录（2026-11-04）

本次将 `bilibili-asr-archive` 固定提交 `cd09dd9ba79c7cbe9c818544a8b85051c3dea365` 中适用于独立处理仓库的更新同步到 `batch-transcribe-proofread`。来源是已提交的 Git 快照；上游工作区的未提交修改不在本次范围内。

初次拆分基准为 `81fa3c5ec07453b6602663100a872fc156c020a4`，本次下游起点为 `9804665fa0717ac1b219657348d06443e386f17f`。初次拆分时已经包含部分 ASR 工作区修复，因此本次按文件与行为适配，不能直接把两个仓库的全部差异覆盖过来。机器可读的来源、文件哈希和验证记录见 [同步清单](upstream-sync-20261104.json)；初次拆分清单继续保留原义。

## 同步内容

| 范围 | 本次处理能力 | 使用说明 |
| --- | --- | --- |
| 本地 ASR | 可选单调最大分数对齐、解码与原始分数证据、静音边缘覆盖、分隔符和超时处理 | [输出策略](asr/asr-output-policy.md)、[配置](asr/asr-configuration.md) |
| 工作流 | 持久批次、固定输入与并发参数、暂停和恢复；Windows supervisor 协作排空；Linux 文件写入观察 | [批次](operations/workflow-batches.md)、[目标选择](guides/workflow-selection.md) |
| 云转录 | DashScope / Qwen 文件转录，以及对已有归档或离线清单中的 YouTube 目标进行批处理 | [云转录](guides/youtube-bailian-transcription.md) |
| 存储与恢复 | S3 / R2 兼容后端、分片上传、远端恢复、引用备份及数据库存储报告 | [S3](operations/s3-storage.md)、[存储报告](operations/database-storage-report.md) |
| 部署与交付 | production 配置、运行和备份入口；portable bundle；delivery 导出、检查和列表 | `batch-transcribe production --help`、`batch-transcribe delivery --help` |
| 契约 | 工作流批次和 publication package 的独立扩展；artifact storage v2 与注册契约资源 | [契约登记表](contracts/registry.json)、[显式升级](operations/archive-upgrades.md) |

ASR 的新增对齐策略需要明确配置，默认策略不变。时间戳与质量门禁继续拒绝不合格结果，不排序或钳制原始时间戳来掩盖失败。历史 attempts、失败证据和 retry holds 不因同步而重建。

## 独立仓库边界

`input import` 仍是新增处理输入的入口；`workflow plan --all-parts` 选择已经登记本地音频的部分，只创建 ASR 及相应校对处理链。CLI 与 Python supervisor 均拒绝 acquisition worker 角色。保留 `batch-transcribe` 主入口和 `bili-asr` 兼容入口。

不引入 Bilibili 元数据、字幕或音频采集，也不引入 YouTube 发现、下载及 `yt-dlp`。新增云转录的目标解析器单独拆为 `services/transcription_targets.py`，只解析本地 CSV / JSON / TXT，不导入平台采集适配器。

一次性生产快照脚本 `scripts/export_completed_ai_snapshot.py` 未纳入公共仓库；通用交付通过 delivery / publication / portable bundle 提供。生产档案、模型、音频、凭据、运行证据和临时目录均未作为源文件提交。

原有上游通知配置、工作流、实现、测试和文档保持原有内容。本次同步不把通知升级成自动合并：后续上游变化继续由现有监测发出通知，再按处理边界审核。

## 兼容与验证

在 Windows / Python 3.12 环境中完成以下验证；对应集合和前提由 `scripts/validate_processing.py`、`tests/test_installed_cli.py` 与 [质量管理说明](development/quality-management.md) 定义。

- 扩展处理链验收：1265 passed、9 skipped、1 deselected。之后补入的 ASR batching / quality foundation 集合另有 53 项通过；最终脚本已经包含这些用例。
- 本地输入边界、协作排空、快照与新增 ASR 回归的针对性复验：64 项通过。
- 上游通知单元测试：14 项通过；mypy 的 18 个配置源文件无问题；Ruff 的语法和未定义名称检查通过。
- 契约资源检查：83 个登记契约、47 个 schema，文档镜像与包内权威资源一致。依赖锁检查及 wheel / sdist 构建通过。
- 独立安装验收：6 项通过。使用仓库外的干净虚拟环境和本地 wheel 依赖，验证主入口、兼容入口、新命令、无数据库错误及包内 SQL / JSON 契约资源；临时目录和 uv 缓存显式指向可写位置。
- 四种隔离的旧、新运行库与 universal 归档均可只读打开，探测前后数据库 SHA-256 不变。清单保存每个合成 fixture 的哈希与结果，不包含生产数据。

先前因旧 schema 拒绝而取消选择的 `test_upgrade_target_indexes_survive_full_snapshot_check_and_restore` 已通过，现已重新纳入验收。Windows 仍明确排除 `test_old_library_uses_scoped_bounded_reads_without_a_global_json_scan`：它在初次拆分时已重现 SQLite URI 打开失败；Linux CI 不排除这一项。

Linux inotify 用例交由远端 Ubuntu CI 验证；Windows 不能执行的 Linux 用例保留跳过状态。完整历史套件仍含采集任务图假设，不宣称全部历史测试已通过。

验证使用合成档案、模型替身和本地模拟服务；本次未重新运行真实 GPU 模型、付费 AI 请求或真实 S3 / R2 服务。启用新的数据库扩展前应在恢复副本上显式验证，并保留匹配版本的备份。
