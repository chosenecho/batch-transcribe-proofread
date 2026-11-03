# 校对重放派生索引

`editorial-replay-index/v1` 是可显式安装的性能索引组，包含指纹表达式索引
`editorial_replay_fingerprint_v1` 和 input/chunk 范围索引
`editorial_replay_scope_v1`。它不新增权威内容表，不改原始 call JSON，不修改
已发布 core schema、冻结历史 fixture 或 content fingerprint。

在新库或完成升级后的目标停写时执行：

```sh
python -m bili_asr editorial replay-index --archive-root ISOLATED
```

普通只读访问不会安装索引。安装使用一个 `BEGIN IMMEDIATE` 事务，完整提交或
完整回滚；异常、取消和进程 kill 都不会提交半组索引。只接受精确登记的 DDL。
同名索引定义不同会拒绝，禁止使用 `IF NOT EXISTS` 隐藏该冲突。只剩一半的组也
拒绝普通读取和安装。经检查确认现有 DDL 匹配后，可显式补齐：

```sh
python -m bili_asr editorial replay-index --archive-root ISOLATED --recover
```

恢复不会删除或接受异定义对象，遇到 altered 对象先保留归档并调查。安装之后
重新创建并检查 snapshot；DDL 安装是明确写入，原 upgrade-check receipt 的
安装基线不能当成安装索引后的完整文件证据。保留原 receipt 和新 snapshot。

完整已验证组在 snapshot、restore、migration-source 检查中按精确 DDL 登记。
两个索引保存在数据库备份中；只有通过定义和完整组核验的这些索引从权威
content fingerprint 排除。partial/altered 组拒绝 snapshot。其它同名对象不能借
此豁免被隐藏。

重放优先按保存指纹 `SEARCH`，再校验原请求计算的指纹；若有更新的 legacy 尝试，
以相同 input/chunk 范围查询保留最新匹配语义。找到最终匹配前不会读取大 response。
重复物理尝试原样保留。旧库没有索引时按范围只读兼容，最多取 257 个 request
rows 判断是否超出 256 行保护；超限报 `legacy_replay_scope_exhausted`，不会以
未找到为由再次付费调用。runtime 必须提供 input_id 和 chunk_id；兼容 API 的
无范围读取也有相同上限，建议安装索引或改用有范围接口。

无关 input/chunk 的坏 JSON 不阻断当前请求；实际匹配证据损坏时，错误携带
call_id 和稳定 reason_code。完整性扫描独立执行，以便定位历史异常：

```sh
python -m bili_asr editorial call-integrity --archive-root ISOLATED --limit 100
```

离线验收包含 12000 条无关调用（每条约 4KiB response），查询计划应包含两种
`SEARCH`，命中只取一次 response，并记录查询次数、Python 内存峰值和耗时；
该合成性能证据不代表线上 API 吞吐。另测精确 legacy fallback、混合新旧调用、
中断事务、真正 kill 子进程后恢复、partial 显式恢复及 snapshot 索引保留。
