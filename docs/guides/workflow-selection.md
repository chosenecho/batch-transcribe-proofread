# 按 BVID、分 P 或整个归档规划工作流

先通过 `input import` 导入本地音视频，或使用已有兼容归档。`workflow plan` 只解析库内身份并规划处理任务；缺少登记音频时拒绝规划。

```sh
batch-transcribe workflow plan --archive-root archive --bvid BV_EXAMPLE_A
batch-transcribe workflow plan --archive-root archive --bvid BV_EXAMPLE_A --page-index 0
batch-transcribe workflow plan --archive-root archive --part-id 12 --part-id 13 --proofread
batch-transcribe workflow plan --archive-root archive --all-parts --proofread
```

`--bvid` 和 `--part-id` 均可重复，且与 `--all-parts` 互斥。`--all-parts` 选择所有未标记 `gone` 的分段，包括已导入的 YouTube 分段。`--page-index` 是零基索引，仅能与 `--bvid` 一起使用；多视频选择时应用于每个视频。

未指定索引时，BVID 选择所有可处理分 P。被标记 `gone` 的排除项会在摘要中列出。显式选择已经 gone 的分段会报错；未知 ID、空归档和缺少指定分 P 也会报错。选择或配置校验失败时不创建 profile 或 jobs。

规划为每个选中分段创建 ASR，可选 `--proofread` 接上校对和渲染。不会创建字幕采集或音频下载任务。已有参考字幕可以作为冻结输入的一部分，原始证据继续保留。相同身份和配置重复规划复用已有任务。

行为依据：`src/bili_asr/storage/workflow_selection.py`、`services/workflow_application.py`、`workflow_planning.py`；回归见 `tests/test_workflow_selection.py` 和 `tests/test_sync_boundary.py`。
