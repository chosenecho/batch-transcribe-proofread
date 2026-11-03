# 批量转录，批量AI校对工具

将**已经采集到本地的音视频和已有转录档案**批量转录、AI 校对，输出带时间信息的字幕、阅读稿和审核对照稿。

本仓库从 `bilibili-asr-archive` 的处理部分独立而来：保留 Qwen3-ASR、强制对齐、SQLite 任务调度、AI 校对、人工审核、发布、搜索、导出和产物存储；平台元数据抓取、浏览器采集、源站字幕获取、Bilibili／YouTube 下载留在原采集仓库。

## 安装

需要 Python 3.12+、uv，以及 PATH 中的 `ffmpeg` 和 `ffprobe`。

```sh
uv sync --locked --extra dev --python 3.12
uv run --no-sync batch-transcribe --help
```

只使用现有转录进行 AI 校对时，安装基础依赖即可。运行本地 ASR 前，先安装与硬件匹配的 PyTorch，再安装 ASR 依赖：

```sh
uv sync --locked --extra asr --inexact
```

PyTorch 由宿主环境提供；`--inexact` 用于保留独立安装的 GPU 运行时。内部 Python 包名 `bili_asr`、`BILI_*` 环境变量和原归档格式继续保留，便于复用现有模型和档案。`bili-asr` 是兼容命令别名。

## 输入与采集边界

有两种使用方式：

1. 对已有的兼容归档使用 `--archive-root`，直接处理其中登记的音频或转录。
2. 初始化新归档，通过 JSON 清单导入采集端已经准备好的本地音视频。清单保留真实来源身份，目前沿用原格式的 `bilibili` 和 `youtube` 两种来源；不会为无来源文件伪造平台 ID。

```sh
uv run --no-sync batch-transcribe archive init --target-root archive
uv run --no-sync batch-transcribe input import --manifest inputs.json --archive-root archive
```

清单示例见 [examples/inputs.json](examples/inputs.json)。相对音频路径以清单所在目录为基准，支持 WAV、FLAC、MP3、M4A、AAC、OGG、Opus、MP4、WebM 和 MKV。导入会检查音频流、时长和 SHA-256，复制到归档并返回 `part_ids`；不会发起平台请求。

相同输入可重复导入；一个分段已登记不同音频时会拒绝替换，避免改变已冻结任务的输入。已有归档继续保留其失败尝试、历史转录和审核记录。不要复制仍在写入的数据库：迁移时先停止写入并使用归档快照或离线迁移命令。

## 批量转录

将下面的 ID 换为导入结果中的 `part_ids`：

```sh
uv run --no-sync batch-transcribe workflow plan --archive-root archive --part-id 1 --part-id 2 --asr-policy all --proofread
uv run --no-sync batch-transcribe workflow run --archive-root archive --role asr
uv run --no-sync batch-transcribe workflow run --archive-root archive --role editorial
uv run --no-sync batch-transcribe workflow run --archive-root archive --role cpu
uv run --no-sync batch-transcribe workflow status --archive-root archive --jobs
```

`--proofread` 将校对和文稿渲染接到 ASR 之后；省略时只规划转录。处理链为：

```text
已导入音频 → ASR 与强制对齐 → 转录质量检查 → AI 校对 → 文稿渲染
                                 └→ 字幕及转录包
```

任务计划不会创建字幕采集或音频下载任务。输入缺失时会明确失败，不会自动回到平台下载。`cpu` 角色处理转录包生成，`editorial` 角色处理校对和文稿渲染，`asr` 角色处理识别。`workflow supervise` 可按角色启动多个 worker，具体参数查看命令帮助。

模型配置示例（PowerShell）：

```powershell
$env:BILI_ASR_MODEL = 'C:\Models\Qwen3-ASR-1.7B-hf'
$env:BILI_ASR_ALIGNER_MODEL = 'C:\Models\Qwen3-ForcedAligner-0.6B-hf'
$env:BILI_ASR_DEVICE = 'cuda'
$env:BILI_ASR_LANGUAGE = 'Chinese'
$env:DEEPSEEK_API_KEY = '替换为你的密钥'
```

环境变量需要导出到运行进程；CLI 不自动读取 `.env`。完整配置示例见 [.env.example](.env.example)。

## 批量 AI 校对

对已有 ASR 转录批量创建校对任务：

```sh
uv run --no-sync batch-transcribe workflow proofread-many --archive-root archive --part-id 1 --part-id 2
uv run --no-sync batch-transcribe workflow run --archive-root archive --only-editorial
```

该命令会在一个事务中冻结所选输入并排队；任一目标无有效 ASR 转录，整批回滚。相同输入和配置复用原任务。单条转录也可使用 `workflow proofread --part-id 1`，或用 `--base-transcript-id` 精确选择转录版本。

默认使用 DeepSeek；也保留 OpenCode endpoint 和凭据策略。校对结果包含 `ai-draft.md` 和 `review.md`，保留原文、时间、模型参数、疑点与用量信息。校对失败保留尝试历史；重试和修复仍需显式执行。

## 审核、发布与导出

保留原有 `publication`、`editorial`、`search`、`export`、`verify`、`snapshot`、`artifacts` 和远端存储命令。AI 校对生成阅读稿后，仍需按内容哈希审核和显式发布。转录包生成与阅读稿发布是两个独立环节。

```sh
uv run --no-sync batch-transcribe publication --help
uv run --no-sync batch-transcribe editorial --help
uv run --no-sync batch-transcribe verify --archive-root archive
```

## 本次同步增加的处理能力

2026-11-04 已按固定上游提交 `cd09dd9ba79c7cbe9c818544a8b85051c3dea365` 同步处理部分。来源、适配和验证范围见 [同步记录](docs/upstream-sync-20261104.md)。

- [ASR 输出策略](docs/asr/asr-output-policy.md)：显式的有序分数对齐和近静音首尾覆盖证据，默认策略保持兼容。
- [全库选择](docs/guides/workflow-selection.md)及[持久批次](docs/operations/workflow-batches.md)：`--all-parts`、成员冻结、暂停、拆分、恢复和领取并发上限。
- [云端转录](docs/guides/youtube-bailian-transcription.md)：本地文件或已导入的 YouTube 音频，经 DashScope 转录并单独导出，保留失败与续跑证据。
- `production`：统一配置、新任务规划、worker 启停、诊断和离线备份恢复。
- `delivery`：按明确的 revision / transcript ID 导出经检查的交付包，在无数据库环境复核。
- [S3 兼容存储](docs/operations/s3-storage.md)：私有 S3/R2 绑定、分片上传、校验、恢复和引用备份；SQLite 仍保存在本地。
- [数据库存储报告](docs/operations/database-storage-report.md)、发布包独立扩展、可移植稿件包及文件写入观察修复。

```sh
# 对导入的全部分段规划转录及校对
uv run --no-sync batch-transcribe workflow plan --archive-root archive --all-parts --proofread

# 查看统一控制与交付入口
uv run --no-sync batch-transcribe production --help
uv run --no-sync batch-transcribe delivery --help

# 需要 S3/SSH 时安装远端存储依赖
uv sync --locked --extra remote --inexact
```

批次、发布包和 S3 schema 升级均需显式安装或转换，先保存验证过的备份并停止写入；普通读取不会自动改库。云端转录会调用付费服务，密钥通过进程环境或私有文件提供。GPU 实测、付费 API 和真实云存储验收与离线回归分开记录。

## 上游更新通知

本仓库监测上游 bilibili-asr-archive 的所有分支，约每 15 分钟检查一次。有新提交头时，在本仓库的“上游提交监测”Issue 中评论并通知接收人；没有变化时保持安静。由于上游是私有仓库，公开提醒不包含分支名、提交消息或代码。配置、手动测试和调度限制见 [上游通知说明](docs/upstream-monitor.md)。

## 开发与来源

迁移范围、兼容性说明和验证记录见 [docs/extraction.md](docs/extraction.md)，原始文件清单及哈希见 [docs/extraction-manifest.json](docs/extraction-manifest.json)。`docs/asr`、`docs/contracts` 和 `docs/operations` 保留原处理系统的技术参考，其中历史采集说明只用于理解旧档案。

```sh
uv run --no-sync python scripts/validate_processing.py
uv run --no-sync ruff check src scripts tests --select E9,F821,F822,F823
```

代码继续使用 GPL-3.0-only，保留原项目许可证及来源声明；内容文件的许可与代码许可分开，见 [CONTENT-LICENSE.md](CONTENT-LICENSE.md)。
