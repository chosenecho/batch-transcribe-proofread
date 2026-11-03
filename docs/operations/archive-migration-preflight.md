# 旧归档迁移预检

此命令只固定迁移输入与保真基线，不转换数据库、不创建迁移目标、不执行或恢复任务。

```bash
bili-asr archive migration-preflight --source-root /path/to/old-archive
bili-asr archive migration-preflight --source-root /path/to/old-archive \
  --source-artifact-root /path/to/old-artifacts --format text
```

默认输出 JSON；`--format text` 输出摘要。成功返回 0，无法完成预检返回 1 并在 stderr 说明原因。
必须显式指定源目录。该命令不读取 `BILI_ARTIFACT_ROOT` 作为源产物根，避免当前机器的写根误指向旧归档。
存在独立源产物根时应显式传入；读取选择为源产物根优先、源归档根回退。
支持位于归档内独立子目录的产物根；指向管理产物目录（如 `audio/`）或排除目录内部的
重叠布局暂不支持，会明确拒绝，以避免重复扫描和错误归属。祖先路径中的其它条目仍需明确规则。

## 支持范围

固定源基线为 main `9b289570494b5e8f7cc564a7eaa5b2eb2c28c3ad`。
`storage/migration-source-bilibili-v1.json` 保存该基线 66 个结构对象的 DDL 和规范化签名，
包含 38 张权威表及明确允许重建的 transcript FTS 对象契约。
读取器不调用当前 `open_database`、`initialize_schema` 或当前 snapshot 的数据库契约推导。
更多旧 schema 的支持必须通过实际样本和固定契约新增，不能根据缺列猜测。

只支持停止写入并完成 checkpoint 的源：

- 停止所有使用该源的写入者，包括旧程序和跨 Windows/WSL 的直接连接者。
- 有非空 `archive.db-wal` 或 `archive.db-journal` 时拒绝，保留文件原样；预检不执行 checkpoint。
- 有仍有效或没有 lease 的 running workflow job 时拒绝。
- 过期 running job、未结束的采集 run/model call 只计入恢复候选，不修改状态。

服务持有源目录维护独占锁。协调锁在源目录旁边，可能创建/使用该锁文件；数据库与产物保持只读。
该锁只能协调遵守它的程序，源目录持续变化仍会导致预检拒绝。
使用 `immutable=1` 的前提是源已经停止并 checkpoint，不能把它用于忽略活跃 WAL 的在线备份。

## 清单与验证

每张权威表记录行数和 SHA-256，编码包含列名、SQLite 存储类型、精确文本/二进制值和有效 rowid。
指纹可以区分 JSON 空白变化、NULL 与空串、同时间戳记录的插入顺序；报告不输出行内容。
派生 FTS 单独列明，不冒充应复制的权威表。未知表、索引、视图或触发器以及被修改的约束均拒绝。

扫描 `audio/`、`transcripts/`、`documents/`、`subtitles/`、`publications/` 内全部正常文件，
逐文件流式哈希，记录实际选择的根以及被高优先级根遮蔽的同路径文件。
包括所有登记音频、成功 audio attempt 引用、五文件 bundle/marker、AI 双稿和全部历史 release，
不只验证当前公开稿。缺文件、摘要不符、登记关系错误、路径碰撞、链接及未完成暂存均拒绝。
冻结输入与 revision 的身份验证独立于文件登记：合法的未渲染 revision 可以没有产物，
但其 prepared snapshot、输入/revision 摘要与 part/transcript/job 绑定仍必须有效。
每个 edition 的审核事件按顺序核对起始事件、身份、合法状态转换及最终 review 状态；
不能只用最后一条批准事件替代完整历史校验。
文件集合及身份在扫描后重新检查；新 WAL 或扫描期间修改导致失败。

根目录只允许支持的产物目录、源 `archive.db` 和明确列出的排除项。
`.env`、`credentials.json` 不读取内容；`manifest.json` 作为旧操作记录列出；SQLite sidecar 单独列出。
`models/`、`logs/`、`cache/` 只记录目录排除，不递归读取权重、日志或缓存。
其它根条目需要先定义明确保留/排除规则，不能静默丢弃。
报告属于私有运维资料，包含源路径和产物清单，应保存于受控位置，不混入公开 catalog。

报告声明 `conversion_performed=false`、`target_created=false`，并返回源契约、数据库摘要、
源指纹、逐表和文件清单、派生对象、排除/遮蔽记录以及恢复候选计数。
这是显式迁移的输入清单，预检通过不代表目标已经生成。转换与安装见[显式契约升级](archive-upgrades.md)。

## 离线验证样本与后续工作

`tests/fixtures/migration_archive.py` 从已提交的冻结 ZIP 和 JSON 清单原字节展开，不调用当前 writer 或 renderer。
样本包含产物与多版本/多状态历史：
ASR 与字幕、coverage/evidence、共享音频、来源标签、同秒反向 ID 顺序、取消任务及阻塞依赖、
AI 双稿、superseded/withdrawn/current release、较新 draft head 与独立未发布稿，以及双产物根。
既有公开/预览读取和完整快照往返用于确认该样本在当前 v1 中有效。
活跃租约与中断、更多审核/语言组合、真实归档规模仍需后续样本。

固定样本保留旧 typed row、rowid、冻结 JSON 和文件摘要；不能让测试悄悄生成新版源。
生产 worker 仍运行时不进行权威迁移验收，应先取得一致、完整的离线归档副本。
真实旧源和大规模副本的已执行验收见[生产副本升级与恢复记录](../evidence/production-upgrade-validation-2026-10-11.md)；
该记录的日期和来源范围不能代表另一个归档已通过盘点。

文件字节采用流式哈希；当前报告清单仍按表/文件数量占用内存，不宣称整个流程常量内存。

实际目标契约、转换、切换与回退见[显式契约升级](archive-upgrades.md)。
