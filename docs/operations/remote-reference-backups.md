# SSH 目录存储与引用型状态备份

此功能实现 #325 的首个明确后端范围：Linux 上的 OpenSSH、Python 3、普通文件、硬链接原子封存和文件/目录 `fsync`。仅 `ssh-directory/v1` 受支持。没有 S3、WebDAV、云盘或对象存储 API 的隐式兼容声明。

远程上传和恢复是显式维护操作。上传完成后，客户端重新读回整个包并核对 SHA-256，才写入副本 catalog。SSH 返回成功、文件存在或服务端 token 都不能单独证明字节已保存。此版本的远程命令始终保留本地来源，不提供远程自动迁出或自动删除。已有本地/挂载目录的 offload 和完整 snapshot 保持各自的严格约束。

## 绑定目标

安装可选依赖 `pip install 'bili-asr[remote]'`，并通过既有可信方式核验服务器 host key，将其加入 OpenSSH `known_hosts`。后端使用拒绝未知主机的策略，不自动信任新密钥。

远端根目录必须已准备好；后端不创建挂载点或缺失根目录。连接配置只在运行机器保存，例如：

```json
{
  "backend": "ssh-directory/v1",
  "target_id": "cold-ssh",
  "instance_id": null,
  "host": "storage.example.net",
  "port": 22,
  "username": "archive",
  "root": "/srv/archive-storage",
  "password_env": "ARCHIVE_STORAGE_PASSWORD",
  "python_executable": "/usr/bin/python3",
  "timeout": 30,
  "max_bytes_per_second": 10485760
}
```

密码只能引用运行环境变量；配置不接受内联 `password`。也可以设置 `key_filename` 或使用 SSH agent，省略 `password_env`。`known_hosts` 可显式指定额外已核验密钥文件。`python_executable` 默认 `python3`；非交互 shell 未配置 Python PATH 时必须指定绝对路径。

```sh
bili-asr remote prepare --binding ssh-binding.json
```

`prepare` 写入一个带随机持久 `instance_id` 的目标标记，返回该 ID 和实际支持的能力。将返回的 ID 填入本机 binding，随后运行：

```sh
bili-asr remote probe --binding ssh-binding.json
```

目标 ID 与 instance 必须同时匹配。根路径各级拒绝符号链接；远端每次操作核对根的物理身份、instance 及最终文件代次。迁移同一个已核验目标到新路径时只修改本机 binding；替换目标则必须显式准备新 instance。连接主机、绝对路径、私钥路径、环境变量值和临时访问地址不进入包、catalog 或操作 receipt。

## 上传与恢复

先用已有 `artifacts transfer --mode copy` 生成并核验不可变包，再上传：

```sh
bili-asr remote push --archive-root /archives/main --binding ssh-binding.json --package /local-store/packages/artifact-PACKAGE_ID.zip
bili-asr remote restore --archive-root /archives/main --binding ssh-binding.json --object-id SHA256 --storage-key audio/ORIGINAL.m4a --scratch-root /scratch
```

上传按有界块传输并限流，在目标包目录写独占临时文件，核验并同步后用无覆盖硬链接封存。重复相同包核验并复用；同 key 不同字节拒绝覆盖。中断不承诺字节级续传：重建未完成批次，源保留。正常异常会清理本次临时文件；进程或主机被强制终止留下的 `.upload-*` 需要维护人员确认没有活跃上传后处理，不能据此删除源。

范围读取受支持，但不冒充完整对象证明。服务端先核对整个已登记包，然后返回指定范围；客户端整包读取时还会独立核对完整摘要。对象恢复先把一个受批次上限约束的包下载到 scratch，完整核验后使用既有精确字节恢复器。空间不足、鉴权失败、离线、错误 instance 和坏摘要都失败；不重新下载媒体、不重跑模型、不变更历史成功任务。

上传错误分为 authentication、host identity、connection、missing、permission、checksum、interruption、capacity 等类别；报告不回显原始连接异常或凭据。后端不自动重试鉴权失败，也不绕过未知 host key。

## 引用型备份

`archive-reference-backup/v1` 与完整 `archive-snapshot/v1` 是不同格式。引用包始终声明 `self_contained=false`。它保存一致数据库、catalog、未外置的受支持文件，以及每个外部依赖的稳定对象摘要、大小、逻辑 target、包、成员、generation 和包摘要。数据库契约、对象包契约和引用包契约分别登记、分别验证。

```sh
bili-asr reference save --archive-root /archives/main --out /backup/state.zip --holds-file /ops/holds.json --remote-binding ssh-binding.json
bili-asr reference inspect --file /backup/state.zip
bili-asr reference check --file /backup/state.zip
bili-asr reference check --file /backup/state.zip --online --remote-binding ssh-binding.json
bili-asr reference check --file /backup/state.zip --online --storage-target cold-directory=/mounted/store
```

save 在独占维护锁内复制原数据库，不执行 job recovery。已记录的 running、failed、cancelled 状态、attempt 数、冻结 JSON、审核与发布事实保持原样。受支持的活跃归档访问者会阻止维护锁；旧 worker 的执行权不能随一个文件副本自动转移。

离线 inspect/check 验证每个随包文件及依赖与数据库/catalog 的对应关系，不访问网络或目标目录，始终报告 `external_state=unverified`。`--online` 才读取外部包，给出 `complete`、`partial` 或 `unverifiable`、检查时间、明确的摘要范围、阻塞对象和关联任务。在线核验依赖可用不代表本地输入、模型、凭据或 worker 已就绪。

save 完成后的自检、离线 inspect/check/plan 和 `verified_reference` reader
对所有成员流式计算大小/SHA-256，只提取 SQLite 数据库，媒体和普通产物
不落盘。临时文件有效载荷为 manifest 报告的 `read_only_temporary_bytes`
（数据库大小），额外内存是有界读取缓冲和受限清单；此数值不代表整个操作
的 RSS 或文件系统开销。路径、重复成员、ZIP 目录和 bundle marker 仍完整
验证，不以跳过媒体校验换取空间。显式 online 远程包检查另需下载目标包。

只读 plan 可以在恢复空间不足时成功验证并报告 `target_ready=false`；
restore 在创建目标前检查全部内嵌文件所需空间，然后重新验证并提取完整
产物到 staging。只读临时空间不再随内嵌音频体积增长，恢复空间仍必须能
容纳真实安装数据。历史 reference/v1 与新 reference/v2 使用同一校验路径。

远程引用必须在保存时提供相应 binding 的 instance ID；保存只读取该非秘密身份，不连接服务器。在线检查要求重新提供匹配 binding。已登记但过期的副本记录不代表当前字节仍可访问。

运维文件仅接受以下严格白名单，不收集任意目录、环境、令牌、worker 配置或整份监控状态：

```json
{"version":1,"holds":{"part:123":["publication_contract_hold"],"JOB_ID":["operator_investigation"]}}
```

不接受未知字段、访问 URL 或无限长度原因。未提供 holds 时记录为未核验，不能等同于“没有 hold”。已核验的 holds 在恢复目录 `documents/reference-recovery/BACKUP_ID/holds.json` 原样保留，可交给现有 `--holds-file` 接口；未核验状态写入单独的 `holds-unverified.json`，不能作为空 holds 输入。不要把含凭据的自由文本作为原因。

上述历史 `archive-reference-backup/v1` 只保存 holds，报告
`control_recovery_capability=limited-holds-only` 和未知的 retry ledger；不能用它
证明可重试预算，也不能把 `holds.json` 当作完整 retry 控制文件。

需要完整重试控制时显式使用 `--retry-control-file` 和该 owner 实际使用的
`--retry-control-lock`，且不能同时提供 `--holds-file`。保存注册的
`archive-reference-backup/v2`，只提取 `retry_holds` 与 `retry_ledger`，
整份监控 JSON 的凭据、其他运维字段不进入包；原文件的 SHA-256 作为
来源证据记录。接受严格 typed `retry-control/v1`、已封装的 v2，以及
显式声明此操作的无版本生产 retry 映射；未知版本/时钟声明拒绝。

```sh
bili-asr reference save --archive-root /archives/current --out /backup/retry-state.zip \
  --retry-control-file /ops/state.json --retry-control-lock /ops/retry.lock
bili-asr reference restore --file /backup/retry-state.zip --archive-root /archives/restored
bili-asr retry-maintenance preflight --help
```

恢复报告返回 `control_state_path`，指向
`documents/reference-recovery/BACKUP_ID/retry-control.json`。该 `retry-control/v2`
文档可以直接作为 retry-maintenance preflight 的 `--state` 和 workflow
retry/status 的控制输入。完整保留原原因、次数、上限、deadline、预约 attempt
及 outcome。原因仅接受有界文本或白名单 reason/source/error_code/调查修复引用/
时间/part/transcript/paused 字段；ledger 只接受已登记预算与预约字段。
未知嵌套字段拒绝，不能把令牌放进原因文本。

封装保存原源摘要、白名单投影摘要、内容摘要、数据库快照摘要与所有冻结 job
身份。preflight 对当前归档逐项核验这些 job 的冻结身份，允许以后新增 job，
但原 job 缺失或被替换会阻断；文件路径变化不会冒充归档身份变化。时间只使用
`unix-seconds-utc/v1`，不转换进程 monotonic 时间，读取时本机 Unix 时间早于
捕获时间会阻断重试。保存者持有声明控制锁并前后验证原字节摘要；该摘要表示
记录的原来源，不宣称在未提供原文件时证明源真实性或提供数字签名。

restore 永不自动执行。之后显式调用 retry tick 时继续先扣预算再发送，
更新 projection/content 摘要，保留 original source、scope、clock，
失败和不确定的预约不退款；已有失败 attempts 与 hold 不被清除。

## 恢复和执行权交接

```sh
bili-asr reference plan --file /backup/state.zip --archive-root /archives/restored
bili-asr reference restore --file /backup/state.zip --archive-root /archives/restored --source-workers-stopped
bili-asr remote restore --archive-root /archives/restored --binding new-machine-binding.json --object-id SHA256 --storage-key audio/ORIGINAL.m4a --scratch-root /scratch
bili-asr snapshot doctor --archive-root /archives/restored
```

plan 检查包、目标是否为空及本地随包数据空间，不创建目标。restore 再次验证全部嵌入数据后安装到新/空目录，保留原数据库的所有 typed rows 和 workflow 状态。恢复报告调用既有 doctor，列出缺失输入和环境要求，并始终明确未启动 worker、未 retry、未解除 hold。

`--source-workers-stopped` 记录操作人员的执行权交接声明，不是自动关闭旧 worker 的开关。先停止旧 worker，重新绑定并核验目标，显式恢复所需对象，检查 doctor/readiness，最后由操作人员选择启动新 worker。不存在自动合并分叉数据库或分布式队列的保证。

完整 snapshot 的必需字节检查没有被放宽；引用包不能交给完整 snapshot restore 冒充自包含备份。

## 验证证据

离线测试使用真实子进程执行与 SSH 相同的远端 agent，覆盖临时写入中断、无覆盖幂等、完整/范围读取、目标根/instance/坏摘要/符号链接、鉴权和错误信息脱敏。副本 catalog 的端到端测试验证远程 push 与恢复后原业务表 fingerprints 不变。引用备份测试覆盖离线禁止 socket、完整 typed rows、hold、坏引用拒绝、目标缺失/错身份/损坏、完整 snapshot 不接受引用格式，以及 CLI 操作。

2026-10-11 在用户授权服务器的隔离 `/root/bili-issues-20261011-remote-target/run-*` 目录上，以严格 known_hosts 和交互密码认证完成 11 项真实检查：2 MiB 包上传、限流、fsync/无覆盖封存、客户端回读、源保留、幂等上传、整包/范围下载、中断不封存、同尺寸损坏拒绝、instance 错配、离线目标。未访问生产归档，凭据没有写入脚本、receipt 或仓库。远端 Python 使用显式路径，验证了非交互 shell 没有 `python3` PATH 时的行为。

新增可选依赖为 Paramiko 5.x；锁文件仅加入 Paramiko、bcrypt、cryptography、invoke、PyNaCl，没有升级已有依赖。依据 [Paramiko 5.0 changelog](https://www.paramiko.org/changelog.html)核对不兼容变化，并用当前服务器完成连接验收；弱 SHA-1/RSA 与 DSA 不在此版本支持范围。主机密钥策略遵循[官方客户端接口](https://docs.paramiko.org/en/stable/api/client.html)。

最终集成于全类型产物提交 `bb74922` 后，WSL 七组测试共 87 通过，覆盖新服务、contracts、原完整 snapshot/CLI 和 artifact groups。远端服务端假装成功但回传错误字节的反向测试，在移除客户端 SHA 检查时准确失败；恢复保护后通过。42 个 registry 条目和 22 个 schema 的文档镜像一致，新增代码 ruff 全检查通过。
