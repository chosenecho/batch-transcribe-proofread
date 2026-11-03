# 最终产物来源链

`provenance` 从最终文件 SHA-256、release、edition 或 AI revision 读取私有证据闭包。
读取不会领取任务、执行推理、补采元数据或恢复冷存储。数据库读取使用同一个只读事务视图；
音频存在时流式核对摘要，缺失时保留已记录的身份并显示不可用。

```sh
python -m bili_asr provenance trace --archive-root archive --release-id RELEASE_ID --format json
python -m bili_asr provenance trace --archive-root archive --artifact-sha256 SHA256 --format json
python -m bili_asr provenance export --archive-root archive --edition-id EDITION_ID --out private-chain.json --format json
python -m bili_asr provenance read --file private-chain.json --format json
```

`export` 创建新的 UTF-8 文件，拒绝覆盖现有文件及与来源归档/产物根重叠的路径。
`read` 完全使用导出文件，不需要原数据库、网络、模型或音频目录。文件可以按显式存储操作
加入 `documents/provenance/` 后随完整快照保存；恢复后使用相同命令校验。
音频文件不嵌入来源链，音频可用性只表示导出时的核查结果。

私有审稿导出可以直接带上 `provenance.json`：

```sh
python -m bili_asr editorial export --archive-root archive --revision-id REVISION_ID --edition-id EDITION_ID --out private-review --provenance --format json
```

此选项使用新增的 `private-provenance-v1` manifest profile；默认导出沿用原有 profile。
来源链绑定导出的 edition/review/revision/content hash，目标目录替换前会同时校验来源链和现有
快照的文件摘要。人工改稿保留父 edition，重新校对保留各自冻结输入和请求配置；撤回状态
及相应发布事件也保存在读取当时的闭包中。

`artifact-provenance/v1` 固定 `frozen-input-and-recorded-producers/v1` 选择政策及 reader 版本。
节点分别保存 release、edition、revision、冻结输入、转录段、全部 acquisition producer、
workflow attempt 和冻结 ASR profile、模型调用、所选 chunk checkpoint、平台观测和音频身份。
每个节点摘要绑定原记录与依赖摘要，文件摘要绑定全部根节点与闭包。
离线校验同时检查领域身份，例如 input/revision/content hash、ASR profile 原配置摘要、
checkpoint 所选调用、最终发布渲染摘要、原观测 envelope/payload 摘要。

请求模型名、provider 返回的模型标识、ASR 主模型与 aligner、运行时解析 revision、配置摘要
分别展示。provider alias 无法证明不可变部署版本时明确显示 `unknown`。相同转录正文由不同
模型产生时，闭包保留全部生产者；新自动校对成功 attempt 记录本次选择的精确 ASR attempt/run。
历史输入、已冻结后恢复的调用或显式按转录 ID 准备的输入，未记录选择时显示
`unavailable_unfrozen_history`，不会从最新 ASR 结果推断唯一生产者。

原始平台响应只有冻结 `source_metadata.observationBindings` 绑定的 observation ID 和
payload/envelope 摘要才进入历史来源链。旧输入没有绑定时明确不可用，即便归档中后来保存了
同一视频的新观测，也不会回填。JSON 中的原记录保持原有字段和序列化字符串。
绑定还必须指向成功响应并符合平台、视频及已声明的分 P 作用域；UP 主分页响应必须实际包含
该视频。丢失绑定原文时读取失败，不用后来响应替代。YouTube 新采集写入的 v2 快照可以携带
这种绑定。B 站新采集的 v2 快照同时冻结实际采纳回执与对应原平台回应；既有 v1 和仅人工绑定
原始回应的 v2 快照仍能读取，`metadata_selection.state` 保持 `unknown`。

B 站实际采纳回执使用独立的 `metadata-acceptance` 节点，owner 为 `application`、codec 为
`metadata-acceptance/v1`，依赖回执中精确指定的 provider 节点。离线读取不查询当前缓存：
它检查原文 ID、双摘要和完整作用域，使用对应版本的纯规范化器重算字段值和 JSON pointer，
再验证值与冻结来源元数据的投影一致。缺失原回应、额外或不同依赖以及重新封摘要后的字段/
路径/作用域伪造均拒绝读取。私有报告的 `application_acceptance_digests` 和
`metadata_selection.fields` 列出具有采纳证据的字段；`raw_observation_digests` 只列 provider。
其他字段仍可保持选择未知，不能把部分字段的回执解读成所有字段均有来源证明。早期未发布
来源链使用普通 observation 节点承载应用回执时，reader 只从其已冻结的精确闭包解析关系并
执行同样校验，保持原图、原始回应和回执字符串不变，不补查当前或最新观测。

公开摘要通过独立命令生成：

```sh
python -m bili_asr provenance summary --archive-root archive --release-id RELEASE_ID --format json
```

它要求已启用内容发布、当前有效的 published release 和既有内容授权校验。固定白名单只含
format/schema version、release/edition/revision ID、content/artifact/private graph SHA-256 和
published time。私有请求、响应全文、审核记录、原始平台响应及 actor 均不进入公开摘要。
完整来源链文件应按私有证据保管。

闭包上限为 25,000 个节点、64 MiB UTF-8 JSON；文本嵌入、文件读取和 replay/edition 依赖深度
有明确上限，超限会失败。不会静默截断或丢弃证据。损坏的已存在音频/文稿、缺失绑定记录、
未知协议版本、循环、重复 JSON 字段或不一致的依赖均拒绝校验。

节点和文件摘要用于检测变化、核对既有领域身份；它们不是签名或外部真实性证明。
核对某个既有导出时，应与独立保留的 `graph_sha256` 或受信备份 manifest 比对。
同一产物后来新增生产记录、发布事件或发生本地迁出时，重新读取可以得到新的捕获摘要；
已导出的文件保留原捕获视图，离线读取不会用当前状态替换它。
