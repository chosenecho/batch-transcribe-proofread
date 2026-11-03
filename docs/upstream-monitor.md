# 上游提交通知

本仓库追踪 chosenecho/bilibili-asr-archive 的所有已推送分支。监测从启用时的远端状态建立基线，之后检测到新的提交头就向本仓库的固定追踪 Issue 发布评论，并提及配置中的通知接收人。现有历史提交不补发通知。

## 已配置的行为

- GitHub Actions 工作流：Upstream commit notifications。
- 每小时第 7、22、37、52 分钟检查，也可在 Actions 页面手动运行。
- 追踪 Issue 和接收人由 [.github/upstream-monitor.json](../.github/upstream-monitor.json) 配置。
- 没有变化时不发评论；仅删除分支也不发评论。
- 同次检测的多个变化合并成一条通知。通知成功后才推进状态；状态写入失败后重跑不会重复发表评论。
- 仅通知，不拉取工作树、不合并代码，也不把上游采集功能重新带入本仓库。

本地也添加了 upstream Git remote，供后续明确选择改动时使用。

## 私有上游与公开下游

启用时，上游是私有仓库，本仓库是公开仓库。因此通知固定采用脱敏模式：不公开上游分支名、原始提交 ID、提交消息、代码或差异。Issue 的状态标记只保存原始提交 ID 的 SHA-256 摘要；GitHub Actions 日志同样不打印上游原始输出。

这类通知表示“上游出现了未观察到的分支提交头”，需要有上游权限的人进一步查看。分支间共享同一个提交头不会产生重复提醒；两次检查之间产生又被移除的临时提交可能不会被观察到。

上游访问使用专用的只读 deploy key，标题为 batch-transcribe-proofread upstream monitor (read-only)。公钥登记在上游，私钥仅保存在本仓库 Actions secret UPSTREAM_SSH_KEY；没有把个人 PAT 放入仓库。该密钥不能写上游，监测也不需要修改上游的代码或工作流。

GitHub SSH 主机公钥来自 GitHub 官方 metadata API，保存在 [.github/upstream-known-hosts](../.github/upstream-known-hosts)。连接强制检查主机身份。上游读取与本仓库 Issue 写入使用不同凭据；Issue 使用本仓库的短期 GITHUB_TOKEN，权限为 contents: read 和 issues: write。

## 查看与验证

在本仓库的 Issues 中查看“上游提交监测：bilibili-asr-archive”。接收人已被分配到该 Issue，并会在更新评论中被提及。GitHub 站内或邮件投递取决于接收人的通知设置。

手动运行工作流并勾选 test_notification 可以发送清楚标记的测试评论；它不冒充上游新提交。正常检查无变化时，不写入 Issue，也不产生测试评论。

本地验证不需要密钥或网络：

    uv run --no-sync python -m unittest tests.test_upstream_monitor -v

测试覆盖更新检测、只删除分支时保持安静、私有信息脱敏、分页去重，以及通知成功但状态更新失败后的安全重试。

## 调度和维护

GitHub 的定时工作流可能延迟，不是实时 webhook。公开仓库连续 60 天没有活动时，GitHub 可能自动禁用定时工作流；届时需要在 Actions 页面重新启用。工作流停用或访问失败期间不会推进基线，恢复后会比较可见的最新提交头。

需要停止通知时，在 Actions 页面禁用该工作流。需要轮换密钥时，替换 UPSTREAM_SSH_KEY 并更新上游对应 deploy key，再手动运行测试。不要删除追踪 Issue 中的状态标记；缺失或损坏时监测会明确失败，避免静默重置基线或泄漏上游信息。

官方参考：

- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys
- https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication
