# 独立仓库迁移说明

迁移日期：2026-11-03（Asia/Hong_Kong）。

## 来源与范围

来源为 `bilibili-asr-archive` 当前工作区，基准提交为 `81fa3c5ec07453b6602663100a872fc156c020a4`。快照包含工作区中正在使用的 ASR 对齐和口头语质量修复及对应测试。新仓库从独立历史开始，不复制旧 `.git`，避免将采集代码留在新仓库历史中。原仓库、原暂存区和生产档案均未迁移或删除。

`extraction-manifest.json` 中的 `included` 哈希记录复制时的源文件；之后为切开依赖而产生的适配以本仓库 Git 提交为准。未复制实际档案、下载音频、数据库、导出成品、模型、`.env`、凭据、生产证据和临时工作区。

## 采集留在原仓库

- Bilibili API 网关、客户端、元数据分页与刷新、字幕采集、音频下载。
- YouTube 源适配器、集合发现与下载、yt-dlp 依赖。
- 浏览器采集脚本及用户动态、专栏、合集、系列采集。
- 对应联网采集 CLI 与旧采集／下载接缝测试。

新仓库不提供 `fetch-meta`、`fetch-tags`、`source` 或 `user-content` 命令；`runs` 仅供离线读取历史采集记录。

## 保留与适配

- ASR、强制对齐、分块、推理诊断、性能与超时配置。
- 质量检查、证据保留、显式修复、任务租约、取消与恢复。
- AI 校对、输入冻结、修订、调用用量、疑点复核和文稿渲染。
- 人工稿件、按哈希审核、显式发布、搜索和导出。
- 归档、快照、离线迁移、产物目录、远端存储及保留策略。
- 原数据库 schema、Python 包名、环境变量及来源 DTO／离线元数据 codec 保留，以读取现有归档；这些兼容类型不执行平台请求。

新增 `input import` 接收本地采集清单；使用实际平台身份，校验完整批次与音频哈希后登记。默认任务规划只生成 ASR 及其校对链，不再生成采集任务。worker 的角色和默认任务种类不领取历史采集任务；历史 ASR 若仍有成功音频前置任务，继续使用其精确输入证据。新的 ASR 使用唯一的已登记音频身份，遇到多个不同音频身份会拒绝猜测。

新增 `workflow proofread-many` 在同一事务内冻结输入并创建校对及渲染任务，任一目标无可用 ASR 时回滚整批。

## 验证范围

`python scripts/validate_processing.py` 是显式的处理链验收集合，包含实际 FFprobe 检查的本地输入测试，以及替代 GPU／AI 服务的离线端到端测试。它不消耗付费 AI 配额、不下载模型、不触碰生产档案。真实硬件模型加载和真实 AI 服务未在此次迁移中重新执行。

保留的完整历史测试集还包含旧的“字幕 + 音频 + ASR”任务图假设，及旧 schema 固化档案测试。它们不属于上述验收集合，也不能被表述为全部通过。首次广泛检查已确认：

- 一批旧测试仍期望采集／下载任务存在，需要按本仓库的新输入边界改写 fixture。
- `tests/test_artifact_catalog.py::test_existing_contract_digest_and_open_do_not_implicitly_install_catalog` 在原工作区也重现 `manuscript-schema-contract` 失败。
- 旧迁移 fixture 还出现缺失 `immutable_publication_packages_delete` 的 schema 拒绝。继续保留严格校验，没有为使迁移测试变绿而绕过 schema 或字幕质量契约。
- `test_editorial_replay_index.py::test_old_library_uses_scoped_bounded_reads_without_a_global_json_scan` 在原工作区和新仓库的 Windows 环境均重现 `sqlite3.OperationalError: unable to open database file`。验收脚本在 Windows 显式取消选择这一用例；旧 schema 升级用例 `test_upgrade_target_indexes_survive_full_snapshot_check_and_restore` 同样不纳入验收。两者仍保留在完整套件中。
- 拆除采集依赖后补充了 Windows 的显式 `tzdata` 依赖，避免原先经采集依赖间接安装的时区数据消失后影响校对调度。

CI 运行明确列出的处理链验收和语法检查。完整历史兼容性套件仍可用 `pytest` 单独执行；本次提交不宣称它已全绿。


## 本地验收记录（2026-11-04）

- 处理链验收：724 passed、2 skipped、2 个明确列出的历史问题取消选择。之后新增的恢复边界测试连同本地输入和批量校对测试共 14 项通过。
- 全部保留测试可正常收集（首次修复后为 3936 项；另增加 2 项恢复边界测试）；这仅表示收集完成，不代表完整历史套件全部通过。
- 语法和未定义名称检查通过；wheel 与源码包构建通过；wheel 在独立虚拟环境中安装后 CLI 帮助验证通过。
- Windows 测试报告明确按 UTF-8 读取；调度所需 tzdata 不再依赖已移除采集包的间接安装。
- 恢复检查明确将历史音频下载／字幕采集任务标为 handler:collection-removed，不再建议安装下载器或提供源站凭据。
- 最终清单的 finalInventory 记录除清单自身外各文件的 Git 暂存内容 SHA-256；included 继续记录复制时的原始文件快照。

## 后续处理功能同步（2026-11-04）

处理能力已按上游固定提交 `cd09dd9ba79c7cbe9c818544a8b85051c3dea365` 更新，范围、适配、来源清单和验证结果见 [本次同步记录](upstream-sync-20261104.md)。以上首次拆分记录是当时快照，不表示当前验收规模。

本次扩展处理链验收为 1265 passed、9 skipped、1 deselected，另补入的 ASR batching / quality foundation 53 项通过。旧 schema 升级与快照用例 `test_upgrade_target_indexes_survive_full_snapshot_check_and_restore` 已通过并重新纳入验收；Windows 的 SQLite URI 旧库读取问题仍按明确用例排除，Linux 不排除。原始拆分清单继续保留，未用本次哈希覆盖其历史记录。
