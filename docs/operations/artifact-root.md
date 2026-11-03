# 归档根目录与产物根目录

`--archive-root` 决定数据库位置。`--artifact-root` 或 `BILI_ARTIFACT_ROOT` 决定文件产物
的位置。默认两者相同；独立产物目录不会改变 `<archive-root>/archive.db` 的位置。

当前数据与恢复规则见 [metadata-storage.md](../metadata-storage.md)，底层路径实现见
[artifact_root.py](../../src/bili_asr/artifact_root.py)。

## 1. 写入与读取布局

```text
archive root/                       artifact root/
└── archive.db                      ├── audio/{stem}.m4a
                                    ├── transcripts/{stem}/...
                                    └── documents/part-{id}/{revision}/{template}/
                                        ├── ai-draft.md
                                        └── review.md
```

| 内容 | 位置与登记方式 |
|---|---|
| 元数据、transcript、job、attempt、editorial、审核状态 | archive root 的 `archive.db` |
| 音频 | write base 的 `audio/`；登记相对 storage key、SHA-256、大小、格式和时长 |
| SRT、VTT、TXT、Markdown、raw JSON 和 marker | write base 的 `transcripts/`；publication 登记相对路径 |
| 阅读与审核 Markdown | write base 的 `documents/`；document artifact 登记相对路径和 SHA-256 |
| 公开阅读站快照 | `publication export --out` 指定的目录，含有效获批 release 与配对 AI 校验参照 |
| 未发布阅读预览 | `publication export-drafts --out` 指定的独立目录，含当前且从未发布的 edition 与配对 AI 校验参照 |
| 私有审阅包 | `editorial export --out` 指定的独立目录 |
| 已发布稿件 | write base 的 `publications/`；release 登记固定路径与 SHA-256 |

每次调用只有一个 write base。读取先探测配置的 artifact root，再探测 archive root；
两者相同只探测一次。旧文件可留在 archive root，新下载、bundle、文档写当前 write base。
音频下载可复用读取回退中合格的旧文件，这不代表新字节写回旧位置。

记录不含绝对根目录。切换配置不复制文件，也不把目录写入数据库。读取只有“当前配置、
archive root”两个位置，不能自动探测此前的多个独立目录。

## 2. 优先级与词法路径

配置顺序为非空 flag、非空 `BILI_ARTIFACT_ROOT`、archive root。空值或全空白值继续
回退。artifact root 配置去除两端空白、展开 `~`，相对路径按进程当前目录转成绝对路径；
archive root 的 `~` 不由该策略展开。

根目录通过 `ArtifactRoots.of()` 保留**词法绝对路径**：使用 `abspath`，不以 `realpath`
抹去符号链接身份。安全检查可以验证实际 containment，但不能把保存的根目录换成
链接目标，绕过 writer 的拒绝规则。

未配置独立目录时沿用单根初始化行为；显式配置为词法相同的 archive root 也属于单根。
最后的文件写入仍经过产品类型自身的路径保护。

## 3. 验证与安全边界

独立根目录必须已存在、可访问且自身不是符号链接。命令不自动创建缺失的独立根，
避免把未挂载的预期位置当成普通新目录。

`workflow run` 在打开数据库和执行任务前验证配置，对独立根进行创建、写入、同步、
删除临时文件的可写探测。读取命令只要求可读；`workflow render` 仅排队，验证可访问，
真正执行时由 `workflow run` 验证可写。

writer 检查所属产品的根、内部目录和目标，拒绝路径逃逸、非法记录路径及危险链接。
音频 key、bundle 路径、文档路径应保持各自的相对布局，不接受任意绝对路径或 `..`。

## 4. WSL 使用示例

先准备根目录，再以同一配置执行和读取：

```sh
mkdir -p /home/chosenecho/bili-products
export BILI_ARTIFACT_ROOT=/home/chosenecho/bili-products
bili-asr fetch-meta --archive-root ./archive --mid 123456 --limit-pages 2
bili-asr workflow plan --archive-root ./archive --part-id 42 --asr-policy all
bili-asr workflow run --archive-root ./archive --limit 20
bili-asr coverage --archive-root ./archive --format json
bili-asr verify --archive-root ./archive --format text
bili-asr export --archive-root ./archive --format json --out ./archive/export.json
```

也可逐次传 flag 覆盖环境变量，以下假设 `/data/bili-products` 已存在并可写：

```sh
bili-asr workflow run --archive-root ./archive --artifact-root /data/bili-products
bili-asr verify --archive-root ./archive --artifact-root /data/bili-products
bili-asr publication export --archive-root ./archive \
  --artifact-root /data/bili-products --out ./reading-site/content
```

flag 属于对应子命令，不是所有命令的顶层参数。`fetch-meta`、`workflow plan` 保存数据库
事实，不需要配置产物目录。

## 5. 校对与确定性渲染

```sh
export BILI_ARTIFACT_ROOT=/data/bili-products
bili-asr workflow proofread --archive-root ./archive --part-id 42 --no-reference
bili-asr workflow run --archive-root ./archive --only-editorial
bili-asr workflow render --archive-root ./archive --revision-id REVISION_ID \
  --artifact-root /data/bili-products
bili-asr workflow run --archive-root ./archive --only-editorial \
  --artifact-root /data/bili-products
bili-asr publication export --archive-root ./archive --out ./reading-site/content
```

`workflow render` 不写 Markdown，也不把根目录固定到 job。后续 run 必须继续提供相同
flag/env；仅在排队时传 flag 不决定执行时的目录。revision 与模板身份保存在 SQLite，
文件路径相对于 write base。

AI 渲染只产生固定的 `ai-draft.md` / `review.md`，不自动创建 edition 或发布。
`publication create` 校验 AI 双稿并冻结完整读者内容；`publication publish` 安装准确获批版本的不可变 `publish.md`。
`publication export` 校验有效 release 的完整内容与文件 SHA-256，输出独立公开快照；
`publication export-drafts` 输出当前且从未发布的 edition 预览。两者同时校验并导出同 AI revision 的原始 `review.md`，
在 catalog v2 登记 `reviewFile` 和 `reviewArtifactSha256`；参照稿不是人工审核记录。
`editorial export` 明确选择 revision / edition，读取基线双稿并生成私有审阅包。人工修改创建新的 edition，
不能直接改已登记文件；见 [出版与审核](../guides/publication.md)。

## 6. 支持入口与当前限制

| 入口 | 行为 |
|---|---|
| `workflow run` | 解析 flag/env、验证可写，传给音频、ASR、发布和 editorial handler |
| `workflow render` | 验证配置，只排队，不持久保存根目录 |
| `coverage`、`verify`、`export` | 合并数据库事实与两个读取根下的 bundle 完整性 |
| `search-index`、`search` | 读取转录及文件补充内容；索引为数据库派生数据 |
| `publication create`、`editorial export` | 读取并验证 AI 双稿、版本内容与来源 |
| `publication publish` | 校验可写，安装固定 release 文件 |
| `publication export` | 读取并校验有效 release 与配对 AI 参照稿，生成公开快照 |
| `publication export-drafts` | 读取并校验当前且从未发布的 edition 与配对 AI 参照稿，生成未发布预览 |
| `dedup --artifact-root PATH report` | 配置放在 dedup 命令层；报告只读 |

当前 workflow 未接入音频预算和自动回收 CLI 选项；旧说明中的 `--max-audio-gb`、
`--keep-audio`、`BILI_KEEP_AUDIO` 不能控制当前 workflow。独立目录改变存储位置，
不提供磁盘配额或保留周期。

## 7. 切换目录与诊断

从单根切到独立根可保留旧文件，也可在停止 worker 后自行整理，保持相对路径和文件字节。
数据库保持原位。切到第三处时，之前独立目录不会自动被探测，应整理到当前两个读取位置。

| 现象 | 处理 |
|---|---|
| 配置错误，退出 1 | 检查存在性、权限、符号链接及 flag/env 优先级 |
| 排队成功，run 写 archive root | 给 run 同一配置，或统一用环境变量 |
| 发布内容被识别为待办 | 确认读取根配置，检查完整 marker 与 bundle |
| 导出哈希不一致 | 检查字节改动，通过 edition 流程保存人工修订 |
| 新位置没有旧文件副本 | 配置不迁移文件，archive root 回退仍能读取原字节 |
| 换目录后音频缺失 | 按记录相对 key 恢复到读取根，或重新获取 |

schema 重建会丢失数据库事实，需要重新采集；这与目录切换不同。恢复步骤见
[metadata-storage.md](../metadata-storage.md#数据库打开与-schema-边界)。
