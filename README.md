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

## 开发与来源

迁移范围、兼容性说明和验证记录见 [docs/extraction.md](docs/extraction.md)，原始文件清单及哈希见 [docs/extraction-manifest.json](docs/extraction-manifest.json)。`docs/asr`、`docs/contracts` 和 `docs/operations` 保留原处理系统的技术参考，其中历史采集说明只用于理解旧档案。

```sh
uv run --no-sync python scripts/validate_processing.py
uv run --no-sync ruff check src scripts tests --select E9,F821,F822,F823
```

代码继续使用 GPL-3.0-only，保留原项目许可证及来源声明；内容文件的许可与代码许可分开，见 [CONTENT-LICENSE.md](CONTENT-LICENSE.md)。
