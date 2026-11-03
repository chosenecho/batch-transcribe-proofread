# 处理仓库的验证与发布

本仓库的 CI 定义在 [processing.yml](../../.github/workflows/processing.yml)。当前门禁运行 Ruff 语法与未定义名称检查、上游监测单元测试、显式处理链验收，以及 wheel / sdist 构建。不能把上游的完整 CI、覆盖率门槛或生产发布状态视为本仓库已经执行的检查。

```sh
uv sync --locked --extra dev --extra remote --python 3.12
uv run --no-sync ruff check src scripts tests --select E9,F821,F822,F823
uv run --no-sync python -m unittest tests.test_upstream_monitor
uv run --no-sync python scripts/validate_processing.py
uv build --no-sources
```

验收集合由 `scripts/validate_processing.py` 明确列出，覆盖本地导入、ASR、校对、批次、发布、S3、备份和恢复。mock 服务只能验证协议及失败边界，不能证明真实模型质量、GPU 吞吐或供应商当前规格。Linux 文件观察与 Windows 排空行为应分别在对应系统验证。

完整历史测试仍保留部分采集任务图假设。验收不宣称整个历史套件全绿；已知排除和实际结果记录在 [迁移说明](../extraction.md) 与本次同步记录中。涉及新功能时补输入、拒绝路径、历史证据保留和恢复测试。

公开同步只取固定提交中的处理代码及必要契约，保留本地输入边界。不要复制生产档案、音频、模型、凭据或一次性运维快照。上游通知机制见 [通知说明](../upstream-monitor.md)。

实机更新前保留匹配的旧环境和经核验的完整备份，停止新增领取并排空写入者。在恢复副本上验证显式 schema 扩展，再切换 worker。代码回退不撤销数据库扩展；原始失败、尝试、holds 和调用账本继续保留。
