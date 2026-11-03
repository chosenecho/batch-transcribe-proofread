# 产物分层、手动迁出与恢复

产物存储支持音频迁出与恢复、完整文本组、外部产物快照和显式在线策略。
生产状态继续由现有 `archive.db` 中的业务表管理；外部音频包与本地工作副本由可选的
`artifact-storage-v1` catalog 登记。迁出不会将已成功任务改成未完成，也不会清空失败次数、重试记录、审核或发布历史。

## 理由、目的和目标

音频占用生产盘，却不是生产进度本身。把任务成功等同于“当前机器这个路径存在”，会使空间释放、存储故障、换盘和新增 ASR profile 都影响生产状态。
另一方面，只复制文件不能释放空间，只删除文件会使下一次消费丢失输入；同名路径被覆盖也不能证明旧版本仍保存。

手动音频迁出先观察实际副本、冻结可核验计划，再流式写入目标、重新读取目标核验、登记外部副本、条件释放源，最后可以恢复同一 SHA-256 的输入。完整文本组和在线策略在相同的身份与核验边界内执行，具体限制见下文。
计划不是删除授权，执行时必须再次观察消费者、保留条件、源字节和目标身份。

层次分工如下：

| 层 | 保存的事实 | 当前实现 |
| --- | --- | --- |
| 生产状态 | jobs、attempts、转录、冻结编辑输入、审核与 release | 原业务表和历史 JSON 保持原语义 |
| 产物身份 | SHA-256、字节大小、不可变分组与业务引用 | `artifact_objects`、音频 binding、不可变 group 数据模型 |
| 存储副本 | 逻辑 target、相对 key、包、核验记录、代次与存在观察 | 可选 catalog；不保存机器绝对根路径或凭据 |
| 本地访问 | 本次 invocation 的 archive/artifact root、本地路径、显式恢复 | 严格查找已登记音频身份；缺失时要求预恢复 |

本次没有启用生产在线自动迁出。自动策略、消费租约和容量背压在
[#324](https://github.com/SuperCatQR/bilibili-asr-archive/issues/324)；非音频版本绑定已实现
[#322](https://github.com/SuperCatQR/bilibili-asr-archive/issues/322)。

## 维护窗口与显式升级

盘点可对现有 `bilibili-v1` 或 `universal-v2` 归档直接运行。写入副本 catalog 则需要显式升级。
升级停止源归档上的使用者，持有独占维护锁，将数据库和现有受支持产物保真复制到独立的新目录。
它不会就地改变源库，也不会执行 interrupted-job recovery；原表的 typed rows、rowid 和冻结 JSON 会比较 fingerprints。
缺失或被覆盖的音频只报告事实，不标成核验成功。升级报告保存在 `documents/artifact-upgrades/`，数据库登记其摘要。

下面的路径是示例，目标目录与源目录都应按实际环境选择。

```powershell
bili-asr artifacts upgrade --archive-root C:/archives/production --target-root D:/archives/catalog --dry-run
bili-asr artifacts upgrade --archive-root C:/archives/production --target-root D:/archives/catalog
```

目标必须是新目录或空目录，且不能与源 archive/artifact root 相互包含。
原归档使用独立 `--artifact-root` 时，升级命令也应传入该配置；升级后的目标把保留文件放在新 archive root 内。
升级需要新目标具有数据库和保留文件的空间。先检查报告，再让生产命令使用新 `--archive-root`。
这个功能不自动切换生产服务的配置。

同一 source baseline 重复升级到同一目标会核验并复用；源事实、源文件、目标报告或目标保留文件变化时拒绝复用。
升级不能修复已经丢失的音频，也不能凭当前 mutable bundle 推导出历史版本内容。

配置根与归档根同名但不同字节时，唯一生产声明摘要对应的音频放回原 key，其他字节保存为不可变备用证据。
同一历史音频 key 声明多个摘要时拒绝升级，要求先明确历史输入版本。备用非音频文件使用 `.preserved` 后缀，避免被识别成活动 bundle marker。
报告的 `snapshot_readiness.reference_bytes_available` 只表示声明路径/摘要可用；
`scope=declared-reference-bytes`、`bundle_integrity_verified=false` 明确它没有验证 bundle 组完整性。
完整快照是否可用仍以实际 `snapshot save` / `snapshot check` 为准。

## 盘点与冻结计划

```powershell
bili-asr artifacts inventory --archive-root D:/archives/catalog
bili-asr artifacts inventory --archive-root D:/archives/catalog --deep --kind audio --no-external-holds
bili-asr artifacts plan --archive-root D:/archives/catalog --kind audio --target-id cold-audio --no-external-holds --out C:/plans/audio-offload.json
```

默认 quick 只观察路径及可读性，不读取音频载荷；deep 才核对 SHA-256 和大小。
支持重复的 `--kind`、`--platform`、`--creator-id`、`--video-id`、`--part-id` 和 `--version` 筛选，以及深度扫描的 `--max-bytes-per-second` 限速。
`kind` 包括 `audio`、`bundle`、`document`、`release`、`source-evidence` 和 `migration`。
文本类型必须先捕获为不可变完整组，transfer 才接受对应计划。
报告输出必须在两个源 root 之外，且不覆盖已有文件；也可以直接取 stdout JSON。

占用指标分别描述路径内容、按 device/inode 去重的物理内容、平台可提供的分配空间、已登记逻辑字节、候选释放内容、目标载荷和单对象恢复工作空间。
内容字节不能保证等于文件系统实际回收空间：压缩、稀疏文件、reflink 和仍打开的 descriptor 都可能不同。
硬链接只有已观察范围内所有链接都可释放时才估算可回收；未知范围外链接会阻止候选。

外部运维 hold 未核验时，报告 `external_hold_unverified`，冻结计划不产生可释放候选。
`--no-external-holds` 表示操作人员已经检查外部调查/重试暂留来源且确认不存在 hold。
有 hold 时提供版本化文件：

```json
{
  "version": 1,
  "holds": {
    "part:123": ["调查期间保留输入"],
    "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef": ["手动暂留"]
  }
}
```

```powershell
bili-asr artifacts plan --archive-root D:/archives/catalog --kind audio --target-id cold-audio --holds-file C:/plans/holds.json --out C:/plans/audio-held-plan.json
```

hold key 可为 job ID、`part:<id>`、`sha256:<hash>` 或 `path:<relative-key>`。
显式空 `holds` 是已检查且没有 hold；未知状态不会被当作空状态。
执行时重新读取 holds 文件，因此旧计划不能覆盖新的 hold。
active persistent pin、共享音频的其他 profile、选择范围外消费者，以及 queued/running/failed/cancelled 任务都会暂留输入。
失败状态、取消状态和 hold 需要后续单独处理，显式选择不会绕过这些保护。

计划包含源 root/path、指纹、SHA-256、大小、逻辑版本、暂留理由和摘要。
源绝对路径只属于本次机器的计划，不进入副本 catalog。移动工作目录后应重新盘点和生成计划。
历史 bundle 路径目前会被覆盖，盘点会报告 `mutable_bundle_version_unverified`，不会把当前文件当成旧版本副本。

## 完整文本组和历史版本

```powershell
bili-asr artifacts capture-groups --archive-root D:/archives/catalog
bili-asr artifacts plan --archive-root D:/archives/catalog --kind bundle --kind document --kind release --kind source-evidence --target-id cold-audio --no-external-holds --out C:/plans/text-offload.json
bili-asr artifacts transfer --archive-root D:/archives/catalog --plan C:/plans/text-offload.json --target-root E:/audio-store --mode offload --no-external-holds
bili-asr artifacts restore-group --archive-root D:/archives/catalog --group-id GROUP_SHA256 --storage-target cold-audio=E:/audio-store
bili-asr artifacts status --archive-root D:/archives/catalog --check-targets --storage-target cold-audio=E:/audio-store
```

`capture-groups` 验证当前 transcript bundle 的完整数据成员和完成 marker：v2 为五个文件，v3 为五个转录文件加 `source-metadata.json`；同时将 raw segments 与数据库中实际发布的 transcript 对应。无法证明的旧版只报告阻碍，不制造历史字节。
AI 成稿和审阅稿、来源正文和审阅证据各自成对；所有 published/superseded/withdrawn release、迁移证据均按原身份保留。
确认的组先保存到 `documents/artifact-objects/<SHA256>`，再登记不可变成员及原始路径。
启用 catalog 后的 workflow publish 在覆盖旧槽位前保存可验证的旧组，并在发布提交中登记新组。v3 组把来源 sidecar 作为独立不可变成员保存；现有五个 artifact_json/public 路径键仍按原身份登记，不能用 sidecar 替换或重命名它们。
计划同时覆盖原始位置和不可变对象副本；任何组缺失成员或摘要不符都阻止迁出。

`restore-group` 先在目标工作盘暂存并校验完整组，最后才安装完成 marker。已有不同版本拒绝覆盖；损坏包不会产生部分完成标记。
完整 snapshot 同时携带当前发布槽位与已保留的所有历史对象，从外部包直接流式读取，恢复后不依赖原 target。
生产完成、历史核验证据、本地驻留和消费 readiness 分别报告。状态和 coverage 不会因正常外置变回未生产；默认状态不探测远端。
`--check-targets` 只检查目标身份和包清单，显示 `storage_unavailable`；不会展开音频或文本，也不会启动模型。
`verified_at` 是历史校验时间，`target_availability=unchecked` 不承诺该目标此刻在线。

## 目标身份与 copy/offload

先准备独立存储目录，再显式绑定逻辑 target 身份。绑定只写该目录的
`.bili-asr-storage-target.json`；不创建缺失的根目录，不改写已有身份。
target 与 archive/artifact root 不能相互包含，路径不接受符号链接或 Windows reparse points。

```powershell
bili-asr artifacts bind-target --archive-root D:/archives/catalog --target-id cold-audio --target-root E:/audio-store
bili-asr artifacts transfer --archive-root D:/archives/catalog --plan C:/plans/audio-offload.json --target-root E:/audio-store --no-external-holds
bili-asr artifacts transfer --archive-root D:/archives/catalog --plan C:/plans/audio-offload.json --target-root E:/audio-store --mode offload --no-external-holds
```

默认 `copy` 保留本地输入。`offload` 在核验和登记后释放计划选中的本地音频副本。
两者都有独占维护锁；生产进程或读者持有受支持的归档访问锁时会拒绝执行。
外部程序直接改数据库/音频，或自行复制身份 marker，不属于该锁协议；维护期间应保持源和目标目录稳定。

包在目标目录 staging 中流式生成，不在生产盘先生成整个大包。
使用 ZIP_STORED 与 ZIP64；音频通常已经压缩，再压缩不会被当成空间收益。
默认每批最多 1 GiB / 1000 对象，可用 `--max-package-bytes` 和 `--max-package-objects` 调整。
单个超限对象独立成包。相同摘要只装一次，多个实际本地路径仍逐个登记释放意图。
目标需要载荷以及包/临时发布开销的空间。

每个包固定 operation ID、plan hash、batch、对象摘要和源代次；成员采用 `objects/<sha256>`。
目标重新读取每个成员和整个容器，核验后无覆盖封存到 `packages/artifact-<id>.zip`。
只有完成目标核验的副本进入 catalog；登记后再次检查本地身份和保留条件，持久化每个路径的释放意图，隔离并验证源文件，删除前再次确认 target。
同名新字节、目标身份不符、离线、坏包、空间不足和新 hold 都阻止释放。

结果区分 `verified_payload_bytes`、包文件大小、`released_copies` 和 `released_bytes_this_run`。
最后一个是本次确认删除最后硬链接的内容字节，不承诺文件系统 free-space 立即增加。
中断后观察到源和隔离文件都已不存在时，本次释放字节计零，不猜测上一次进程释放了多少。

## 检查、恢复与生产继续

```powershell
bili-asr artifacts check --package E:/audio-store/packages/artifact-PACKAGE_ID.zip
bili-asr artifacts restore --archive-root D:/archives/catalog --target-id cold-audio --target-root E:/audio-store --object-id SHA256 --storage-key audio/ORIGINAL_NAME.m4a
```

`check` 重新读取容器及全部成员，可加 `--expected-sha256` 对比已知容器摘要。
`restore` 的 object ID 是裸的 64 位小写 SHA-256，去掉 inventory 展示中的 `sha256:` 前缀。
storage key 必须对应保留的音频身份，恢复到本次 `roots.write_base` 的音频目录。
恢复只读取所需成员，临时文件核验通过后无覆盖安装；已有同字节文件可复用，不同字节拒绝覆盖。
本次恢复核验成员，`container_reverified=false` 明确它没有再次读取整个容器；完整复核使用 `check`。
坏或缺失副本可尝试 catalog 中同 target 的其他副本；不会隐式重新下载。

已安装但本地登记失败时，重复 restore 会核验已在场字节后补登记。
新 ASR profile 可消费相同摘要输入，旧成功 audio job/attempt 保持原样。
Bilibili 和 YouTube 生产者、ASR 输入都会检查已登记身份，坏的优先根不能遮住正确的 fallback 副本。
如果已登记输入在所有本地根都不可用，会报告 `artifact_input_unavailable`，要求显式 restore。
仅有 `artifact-storage-v1` 时应先显式恢复再安排新消费。另行安装 `artifact-online-v1` 后，公共 workflow 在 claim 前核对已登记输入，使用明确的 target binding 自动恢复同一 SHA-256；可预见的输入或容量阻塞不会创建业务 attempt。

## 显式在线策略

在线协调是独立的 `artifact-online-v1` 扩展，依赖 `artifact-storage-v1`。
先停止源归档写入，使用 `archive upgrade-plan` / `upgrade-apply` 安装到独立空目标；旧源保持不变。
例如原生 universal 归档指定完整目标组合如下。带 import/supplement 的归档应保留其全部契约，可先查看 `archive upgrade-paths`。

```powershell
bili-asr archive upgrade-plan --source-root D:/archives/catalog --target-root D:/archives/online --target-contract universal-v2 --target-contract artifact-storage-v1 --target-contract artifact-online-v1 --no-external-control-state --output C:/plans/online-upgrade.json
bili-asr archive upgrade-apply --plan C:/plans/online-upgrade.json
bili-asr artifacts policy-show --archive-root D:/archives/online > C:/plans/policy.json
```

没有保存策略时返回完整默认配置，`mode=off`，不安排扫描或迁移。
策略 JSON 对应发布的 `artifact-policy-v1` schema；`policy-set` 要求已安装 online 扩展，不执行 DDL。
独立的压缩音频自动维护使用新的 [audio-retention.md](audio-retention.md) 中 `artifact-policy-v2`，其默认 `allow_delete=false`，必须明确开启删除；旧 v1 契约继续可用。
从默认配置开始，先限定 `selection.kinds=["audio"]`、一个 `selection.part_ids` 或小范围业务选择，配置真实 `target_id`，使用 `mode=copy` 验证目标。
各选择器与手动 inventory/plan 相同；空数组表示不限该维度。

| 配置 | 含义 |
| --- | --- |
| `mode` | `off` 停止安排；`copy` 只增加已核验副本；`offload` 才允许有条件释放 |
| `minimum_age_seconds`、`priority` | 最短本地保留期；按 `oldest` 或 `largest` 排序 |
| `trigger_free_bytes`、`stop_free_bytes` | free space 低于前者开始迁出，达到较高的后者停止；中间保持上次压力状态 |
| `minimum_free_bytes` | 恢复、下载和 ASR 工作空间预留后仍须保留的空间 |
| `batch_bytes`、`batch_objects` | 每次最多选择的唯一载荷字节与对象数；超限的完整连通组跳过，不拆散组 |
| `max_concurrency` | 同策略最多 1–8 个执行 lane；同对象仍只允许一个迁移者 |
| `bytes_per_second` | 每次执行的载荷读取限速，包含扫描、复制及重复核验；0 显式不限速 |
| `scan_interval_seconds`、`backoff_seconds` | 成功后扫描间隔、无候选或失败后退避；后者不能小于前者 |
| `download_bytes_per_second`、`download_workspace_multiplier` | 按时长估计下载峰值，乘数同时覆盖 staging、转换和安装工作空间 |

```powershell
bili-asr artifacts policy-set --archive-root D:/archives/online --config C:/plans/policy.json
bili-asr artifacts policy-run --archive-root D:/archives/online --target-root E:/audio-store --holds-file C:/ops/holds.json
bili-asr artifacts policy-run --archive-root D:/archives/online --target-root E:/audio-store --holds-file C:/ops/holds.json --watch --drain-file C:/ops/offload.stop
bili-asr workflow run --archive-root D:/archives/online --storage-target cold-audio=E:/audio-store
```

每次策略执行保存配置摘要、完整配置、冻结 plan、结果和下一次允许运行时间。
调整配置不会扩大旧计划；批次开始和最后删除前再次核对当前配置摘要，变更或停用可中断未完成释放。
复用手动 planner/executor 和同一包/释放意图协议。进程退出后锁由操作系统释放；重启在退避期限后复用原计划对账，真实释放字节只统计本次确认删除的最后硬链接。
完成 copy 核验后再将策略配置改成 `offload`。上线从一个 part、单 lane、小批次开始，保留人工 hold 文件和独立完整快照，并核对报告中的 `scan.bytes_read`、`io.bytes_read`、耗时及 `released_bytes`。
这些测量包含重复核验成本，不承诺固定速度或 free-space 增量。

读取与迁移通过跨进程共享/独占内核锁协调，同对象的多个 profile 可以同时读。
read pin 从 claim 前核验/恢复持续到 handler 与业务提交结束；实际解码子进程另持对象锁并核对 SHA-256，即便父进程失去执行租约也不会因计时过期被回收。
自动清理只处理已无内核 reader 的运行时 pin，人工 pin/hold 永不自动解除。
发布覆盖与迁出共同锁住 part 的可变发布槽，迁移还锁定冻结对象身份，并在最终删除前用短数据库写锁复验队列/pin，防止新消费者插入最后观察窗口。
POSIX 使用 flock、Windows 使用 LockFileEx；释放仍以具体 inode/文件标识、大小、mtime/ctime 和摘要核验约束源代次。
支持的归档访问者必须使用这些边界；外部程序绕过锁直接修改源或目标目录仍应停止后人工核查。

公共 workflow 的 audio/ASR 候选在 claim 前检查本地字节，缺失时按显式 `--storage-target ID=PATH` 绑定恢复准确对象。
恢复按对象去重，并与下载及 ASR 解码工作空间共享磁盘预留。目标离线/损坏、未知下载时长、磁盘压力或剩余预留不足时记录独立 `artifact_input_states`，保留 queued 状态和 attempt_count，60 秒观察退避内跳过它，其他可执行任务继续运行。
实际恢复操作仍单独登记 `artifact_transfers`；业务执行开始后的真实失败仍保留实际 attempt 证据。
`workflow supervise` 同样接受 storage target binding；目录位置仅属于当前运行配置，策略和快照不携带机器路径或凭据。

预留按已知恢复成员大小、ASR PCM 工作量、配置的下载时长上界估计计算；它是协作进程的 admission control，不是文件系统硬配额。
数据库预留与 ASR 私有输入的文件回执共享 `capacity` 内核锁，双方都计入两个账本的活跃字节；旧归档根与配置产物根的回执保守合计。未安装 online 扩展的归档仍使用私有输入回执；读取空间预算不安装任何 schema。只有对应 owner 内核锁已释放时才能退休预留，不按租约、PID、年龄或定时器猜测。详见 [ASR 输入身份与临时空间](audio-retention-policy.md#asr-的输入身份与临时空间)。
应按实际 provider 最大码率调高下载系数并保留空间余量，外部进程的磁盘写入仍可能导致真实空间不足。压力未解除时禁止新增大下载；已有本地可用输入、字幕等不依赖新增大文件的工作可继续。
完整 snapshot 保存 online 扩展及审计状态；恢复本身不启动策略或消费任务，也不解除生产 hold。

## 中断对账与边界

```powershell
bili-asr artifacts reconcile --archive-root D:/archives/catalog --plan C:/plans/audio-offload.json --target-root E:/audio-store --no-external-holds
```

| 中断窗口 | 重新执行的行为 |
| --- | --- |
| 未封存批次 | 原输入保留；同计划重新构建未完成批次 |
| 包已封存，未登记 | transfer 重新核验包并补登记 |
| 已登记，未建释放意图 | transfer 重新核对消费者/源，再决定释放 |
| 已持久化意图，隔离/删除未完成 | reconcile 核验外部包、重查暂留、按确定路径对账 |
| 新 hold，隔离文件仍在 | 恢复原输入并取消本次释放；条件清除后可再次对账 |
| 原路径出现新代次 | 保留原路径及隔离文件，报错供检查 |
| 目标不可用或坏包 | 保留源/隔离文件，恢复目标后再次对账 |

copy/offload operation ID 由模式和计划摘要确定，已完成操作重复调用核验外部包并复用结果。
重复调用已经完成的 offload 不会再次删除随后恢复的工作副本；要迁出新本地代次应重新生成计划。
不承诺包内任意断点续写。

完整 snapshot 可通过显式 target 绑定直接流式读取外部包中的必需对象，不必先把音频回填生产盘：

```powershell
bili-asr snapshot save --archive-root D:/archives/catalog --storage-target cold-audio=E:/audio-store --out F:/backup/full.zip
bili-asr snapshot check --file F:/backup/full.zip
bili-asr snapshot restore --file F:/backup/full.zip --archive-root D:/archives/restored
```

每个缺失路径必须有数据库声明的 SHA-256 和 catalog 的准确对象/包身份。
保存时重新读取所需成员并验证 SHA-256；目标离线、身份不符、成员损坏或空间不足均拒绝提交快照。
完整快照仍保留原 v1 格式及严格必需文件校验，包内包含全部所需字节；恢复后读取不依赖原外部盘。
没有 target 绑定时沿用严格本地检查，缺文件仍失败。绑定路径仅用于当前命令，不写进快照。
库外 hold 不在现有完整快照范围内，换机时必须另行交接已经核验的运维控制记录。
非自包含引用型备份由 [#325](https://github.com/SuperCatQR/bilibili-asr-archive/issues/325) 单独定义。

## 验证范围

测试使用隔离的 SQLite 与小字节 fixture，覆盖两类归档契约、源库/原表保真、共享消费者、未知/显式 hold、active pin、硬链接、坏摘要和路径边界。
CLI 闭环与复制/核验/登记/隔离/删除故障注入检查没有外部有效副本时不释放，以及恢复同一字节后可继续新 profile。
在线多进程、进程崩溃和 12 周期 soak 已在 WSL/POSIX 验证。Windows 11 / NTFS 原生 216 项相关回归通过，覆盖真实 LockFileEx、拒绝覆盖的 rename、迁出/恢复、升级和快照；另有 1 项因账户无法创建 symlink 跳过。实际数字、范围及复现脚本见 [2026-10-11 验证记录](../evidence/artifact-online-validation-2026-10-11.md)。多周期 RSS/限速 soak 只在 POSIX 运行，网络文件系统与 Windows symlink 场景未因此获得通过结论。模型和下载使用离线替身，不触发生产任务。
独立审查覆盖正确性、可读性、架构、安全与 I/O；SHA 校验 mutation 证明坏成员和坏输入测试会在保护被移除时失败。
