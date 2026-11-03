# 用 API Key 调用 Qwen Audio 3.1 转录

正式入口为 `batch-transcribe transcribe`，支持本地文件和显式选择的已下载 YouTube 音频。
基础依赖加 PATH 中的 `ffmpeg` / `ffprobe` 即可，无需 GPU、PyTorch 或本地 ASR 模型。
产物是独立的 `asr-cloud` 导出，不自动写入生产转录库；YouTube 入口只读现有归档。

## 凭据和命令

通过进程环境 `DASHSCOPE_API_KEY`，或 `--api-key-file` 指定仅含密钥的私有 UTF-8 文件。
文件优先于环境变量，支持 UTF-8 BOM 和首尾空白。CLI 不自动读取 `.env`，不要提交密钥文件。
命令参数传文件路径，避免密钥出现在命令行历史中。

```sh
# 单文件；省略 --api-key-file 时读取 DASHSCOPE_API_KEY
batch-transcribe transcribe file --audio recording.mp3 --output-root exported/recording --api-key-file /private/dashscope-key.txt

# YouTube 批次：按显式清单转录已下载音频
batch-transcribe transcribe youtube --archive-root downloads/youtube --input selection.csv --output-root exported/youtube-qwen
```

单文件可加 `--expected-sha256` 校验外部记录的 64 位哈希；未指定时计算本地文件哈希。
清单支持 TXT 每行一个 YouTube 单视频 URL / ID、URL 数组 JSON、以及含 `youtube_url`、
`youtubeUrl`、`youtube_id` 或 `url` 字段的 CSV / JSON 对象数组；整体校验并按 ID 去重。
先用 `input import` 导入本地音频，或使用已有归档中登记的音频。此入口不采集元数据、不下载音频；缺失音频记为 `audio_pending`。
`scripts/transcribe_youtube_bailian.py` 保留为 YouTube 命令的兼容包装器。

## 模型与调用

- 精确使用 `qwen-audio-3.1-asr-flash`。
- 单次音频最多 5 分钟，因此按最多 240 秒顺序切成 16 kHz 单声道 PCM WAV，保存音频 SHA-256 和分段偏移。
- 使用北京地域 DashScope 兼容 HTTP 接口，以 Base64 传输音频，开启说话人分离；编号仅在各分段内部有效。
- 模型输出经过润色，不等同于人工核对的逐字稿；按输入/输出 Token 计费。
- 每段保留原始响应、request ID 和用量。超时可能已经计费，不能将网络错误视作未扣费。

以下是上游附带的供应商参考；本次同步只核对实现与离线协议，未重新核验服务规格：

- https://help.aliyun.com/zh/model-studio/qwen-audio-asr-http-api
- https://help.aliyun.com/zh/model-studio/asr-model
- https://help.aliyun.com/zh/model-studio/model-pricing

## 续跑与输出

单文件写入 `--output-root`，YouTube 批次按视频 ID 建子目录。每条音频包含
`request-config.json`、逐段 `chunk-NNNN.json`、`transcript.json` / `transcript.txt`、
时间戳可用时的 SRT/VTT，以及全部分段处理结束后的 `complete.json`。恢复处理期间移除完成标记。
批次另存 `batch-status.json`，区分 `succeeded`、`needs_review`、`audio_pending` 和 `failed`。
只有全部选中目标成功且无需复核时退出码为 0；待复核、缺失音频或失败返回 1。
认证、权限、余额或额度错误会停止后续视频，报告保留所选总数和已处理明细。

成功分段直接复用。失败或中断默认停止，需要核查后显式加 `--retry-failed`；
旧尝试另存为带时间后缀的 JSON。该选项也可重试明确的无词响应。变更模型或输入等配置时拒绝混用缓存。
输出目录带进程锁，防止同一音频被并发重复请求。

原始结果先保存，再校验时间戳。倒退、负数或无效时长不会被排序、钳制或掩盖：
保留完整文本并标记 `requires_review`，不输出部分时间戳字幕，且清理旧的派生字幕。
明确返回 `ASR_RESPONSE_HAVE_NO_WORDS` 时保留原错误、request ID 和区间；这个状态不证明源音频静音。
无词区间之外的有效句子仍可导出字幕，但需要复核，不能视作完整转录。全音轨无词时不导出空字幕。

`complete.json` 只说明所有分段处理结束，仍需结合 `requires_review`、`untranscribed_intervals` 和
`timed_exports_available` 判断可用范围。原始错误和失败尝试保留供复核。
