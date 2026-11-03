# 校对预算、分块候选与有界对照

新规划使用 `token-budget-v2`。`serialized-request-utf8/3-v1` 按完整 canonical
request JSON 的 UTF-8 长度估算 token，包含系统提示、正文、参考、只读上下文、
段 ID、结构与 budget estimate 自身，另留 256 字节 framing。实际请求和追加反馈的
再生成请求在预约物理调用前再次检查。它是版本化估算，不能代替 provider usage
或证明 endpoint 支持 1M。`context_tokens=1048576` 只是冻结配置事实。

输入上限取 `min(max_input_tokens, context_tokens-max_output_tokens-safety_tokens)`。
输出保护保留原先较保守的 UTF-8 reserve，另命名为
`conservative-utf8-output-reserve/v1` 的 `output_reserve_tokens`；它不表示实际输出
token。段数、字符、输出 reserve 和输入 token 都会单独报告限制原因。单段连同
参考、上下文超限时以 `editorial_budget_exceeded` 结束，不拆写、不丢段。

自 2026-10-13 用户确认后，新任务默认 `large-v1`：最多 2048 段、128000 字符，
同时仍受输入、输出和 safety token 限制。能够容纳的整份文本优先一次处理，超限时
继续按冻结预算分块。需要小批时显式指定 `--chunk-preset baseline-v1`，其保护为
256 段、20000 字符。显式 `--max-chunk-segments` /
`--max-chunk-chars` 覆盖所选 preset。规划寻找容量边界后，在末尾 20% 区间优先选择
句末或至少 1200ms 间隔；相邻 chunk 共享配置数量的只读上下文。正文段只能属于
一个 chunk，不能把只读段当正文来源。完整段覆盖、顺序、重复防护、严格输出结构
和已有物理调用预算继续生效。

估算器、preset、边界、截断理由、配置进入冻结 snapshot/input 身份。历史未带该
策略的 input 按 `legacy-v1` 加载，普通重规划复用已保存 chunks；显式刷新才生成
新的输入。已有原始请求、响应、稿件与失败尝试保留。

先对同一个已冻结 input 做只读计划，不产生 API 调用或写入归档：

```sh
python -m bili_asr editorial plan --archive-root ISOLATED --input-id INPUT_SHA256
```

默认报告两个 preset，每块来源 ordinal、预算、容量限制及逻辑边界、预计首次调用数
与最大物理调用数。无带日期的价格证据时费用范围为 `unknown`。600 个普通短段的
离线样本可显示 baseline 3 块、large 1 块，这是规划差异。

实际对照需要已授权的测试 endpoint/key 和独立 ledger；不要从生产进程提取凭据。
未配置可用 key 时只运行计划与离线契约测试，endpoint、usage、质量或费用的实际
结论仍待验证。下面命令每个候选最多首次请求及一次已有 allowlist 反馈再生成；
历史禁用再生成时候选也禁用。ledger 全局最多 4 次，API 不会隐式无限重试。

```sh
python -m bili_asr editorial compare --archive-root ISOLATED --input-id INPUT_SHA256 \
  --ledger PRIVATE/comparison.db --editorial-base-url https://api.deepseek.com \
  --editorial-model TEST_ALIAS --max-physical-calls 4 --max-output-tokens 8192 \
  --allow-paid-calls
python -m bili_asr editorial compare-report --ledger PRIVATE/comparison.db
```

测试样本限 50000 正文加参考字符，物理预算允许 2 到 20 次。预算至少能覆盖两个
候选所有 chunk 的首次调用；小输出上限可能改变切块数量，请先看计划。对很小样本
baseline/large 均一块的实际对照只能验证请求与响应能力，不能证明大块效果。
付费 CLI 还会检查 ASR 结构、异常重复与已保存的 ASR 准入证据；缺证据的老 ASR
保持 unknown，先按 [ASR 显式重验流程](asr-admission-and-repair.md)处理。

专用 SQLite ledger 保存完整冻结来源、两个候选、endpoint/模型与固定总预算。
每次 HTTP 之前持久化 reservation。超时、错误、进程中断或未知发送都消耗预算；
重启不会重发未知请求，不能修改 manifest 或加大原 ledger 预算。原始请求/响应
原样保存，响应 checksum 和逐次请求身份在新增调用前核验。结构失败和被截断的
响应保留；只有 allowlist 引用错误才会进行有界反馈再生成。对照不会写生产任务。

报告区分 requested model 与 returned model，保留原始 usage，累计已知输入/输出/
推理/cache counters，同时列出缺失计数；记录延时、finish_reason、失败、重试和
结构完整性。没给出的字段保持 unknown，不推定 reasoning 为零。默认 preset
为当前新任务默认 large-v1；候选结构成功和模型别名不能证明 1M 能力。

可用 `--pricing PRIVATE/prices.json` 明确冻结价格来源、日期和货币。格式如下；
数值是说明格式的占位价格，不是实际报价，应替换为核验后的证据：

```json
{"currency":"USD","source":"operator verified price evidence","observed_at":"2026-10-13T12:00:00+08:00",
 "input_cache_hit_per_million":0.1,"input_cache_miss_per_million":1,"output_per_million":2}
```

规划费用范围按估算输入 token、所有 chunk 输出上限、最多一次反馈再生成计算，
明确标为估算而非账单上限。实际成本仅在所有调用提供必要 cache 与 completion
counters 时按所给价格计算；缺价格、缺 usage 或未知发送时为 unknown。

完整性、原意、术语、跨块衔接与结构失败都需要记录复核：

```sh
python -m bili_asr editorial compare-review --ledger PRIVATE/comparison.db --out PRIVATE/packet.json
python -m bili_asr editorial record-review --ledger PRIVATE/comparison.db \
  --review PRIVATE/filled-review.json --out PRIVATE/review-record.json
```

packet 是私有输出，含冻结原文、每个候选完整输出及 `review_template`。把模板单独
复制为 filled-review.json，由具名 reviewer 填写带时区日期，每个 rubric 项给出
pass/fail/unknown 和具体证据；审阅跨块衔接时按全局段序检查。没有全部结构输出
不能填写 completeness 或 structure_failures=pass。agent 复核单独记录，不能标成
人工已复核。record 绑定 comparison ID、review hash 与当次 report hash，文件
以独占新建写入，不覆盖既有复核。默认报告的 semantic_quality 仍为 not_reviewed，
具名记录是独立证据。2026-10-13 用户在官方 300 段结构对照和 60 段质量对照后明确决定后续默认大批。
该决定不将 agent 评审标成人工通过；两组未确认宾语补写及语气变化等问题继续保留，
1M 实际上下文能力仍未验证，#412 的质量及能力验收继续开放。已有冻结输入和请求
按保存的 preset/边界读取；缺少历史分块字段时仍按 256/20000 的兼容默认解释。
显式以新大批配置重新规划可能产生新 input 和付费任务，需先查看计划。
