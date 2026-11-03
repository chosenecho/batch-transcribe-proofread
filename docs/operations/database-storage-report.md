# 数据库存储构成报告

`bili-asr archive storage-report` 在一个**只读**连接上统计归档数据库的逻辑
字节、字符数、SQLite 页占用、按月增长、精确重复内容和压缩样本收益。它不写
数据库、不建索引、不 checkpoint、不 VACUUM，也不移动或删除任何证据。

该报告对应 issue #515 的第一步：先给出可复核的存储构成，再决定冷归档方案；
报告本身不执行压缩、迁移或清理。

## 运行

全库扫描会读取所有表页。DELETE 日志模式下长时间读事务会阻塞写者，因此应在
已验证的一致性副本上运行，而不是在仍有 worker 写入的活跃归档上运行：

```sh
bili-asr archive storage-report --archive-root /srv/archive-snapshot > storage-report.json
```

也接受显式数据库文件：

```sh
bili-asr archive storage-report --archive-root /srv/archive-snapshot/archive.db
```

可选参数：

| 参数 | 默认 | 作用 |
| --- | ---: | --- |
| `--samples` | 8 | 每个大字段取多少个值做 zlib 压缩样本（1–64） |
| `--top-tables` | 20 | 对多少个最大表按月统计增长（1–100） |
| `--top-columns` | 8 | 对多少个最大字段做重复与压缩统计（1–100） |

命令只输出 JSON。数据库缺失、契约表缺失或参数越界时以非零状态失败；契约表
缺失只影响 `contracts` 字段，不阻止物理统计。

## 输出字段

- `database`：文件字节、`page_size`、`page_count`、空闲页、`journal_mode`、
  SQLite 版本，以及存在的 `-wal`/`-shm`/`-journal` 旁文件字节。
- `contracts`：归档和稿件契约版本；旧副本可能没有对应表。
- `page_objects`：`dbstat` 按对象（表或索引）汇总的页数与字节；不可用时
  `page_object_bytes_available` 为 `false`。
- `tables`：每表行数、`logical_bytes`、`page_bytes` 和逐字段
  `logical_bytes`/`characters`。文本字段的字节按
  `length(CAST(column AS BLOB))` 统计，字符数按 `length(column)` 统计；两者
  只有在纯 ASCII 时才相等。
- `growth`：按表内 `created_at`/`finished_at` 等时间列，以 UTC 月份汇总行数
  和逻辑字节；无法解析的时间归入 `unknown`。
- `large_columns`：最大的若干字段的精确重复与压缩样本。`duplicate_bytes`
  只计第一次出现之后的重复字节，`duplicate_ratio` 以该字段总字节为分母。
- `limitations`：本次统计的边界，随结果一起保存。

## 解释边界

- 逻辑字节不是 SQLite 页占用：页包含索引、空闲页、B 树和行开销。
- `page_bytes` 只属于表对象；索引在 `page_objects` 中单独列出，不摊回表。
- 重复检测比较字段的完整原始字节，不做语义近似；语义重复不在本报告中。
- 压缩样本用 zlib level 6 对少量值估算，不能当成全库冷归档压缩率。
- 活动库或未完成的副本可能带未 checkpoint 的 WAL 旁文件；旁文件字节会列出，
  但报告不合并其中的已提交记录。需要完整数据时先使用一致性快照。
- 报告只描述统计时点，不是实时值，也不证明任何证据可以删除。

## 与后续工作的关系

本报告不改变冻结输入、请求/响应、修订、attempt、usage 或重试状态。是否采用
可校验压缩、内容寻址 blob、外部对象存储或 GC，需要依据本报告另行设计并显式
迁移；逻辑删除也不会自动缩小 SQLite 文件，VACUUM/repack 需要单独的维护窗口、
锁和备份检查。
