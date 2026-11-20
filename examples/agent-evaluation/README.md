# Agent 对照基准

用同一台 FLMH 模型网关跑两组配对实验。规划组比较模型决策次数和工具调度，记忆组比较整段对话的实际 token 用量和关键约束保留率。仓库提供问题、商品样本与人工标签，测量记录在手动运行时生成。

## 手动运行

1. 使用已就绪的 Java Model Gateway，在它的 `agent_plan` 路由上选定真实供应商和模型。Gemini 可经网关已有的 OpenAI 兼容适配器接入。准备该网关认可的用户 JWT，以及部署要求的服务密钥。每次响应都会核对 `source=OPENAI_COMPATIBLE` 和指定的 `provider/model`。这些标识来自网关，供应商内部的实际路由可结合其日志核对。
2. 在 PowerShell 进入 Python 项目。以下命令使用项目已有解释器。输入 JWT 时只填令牌值。启用服务鉴权的网关还需在同一终端设置 `$env:MODEL_GATEWAY_API_KEY`。

   ```powershell
   Set-Location -LiteralPath 'D:\Happy\芳\flmh-p'
   $py = 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe'
   $env:MODEL_GATEWAY_URL = 'http://127.0.0.1:8082'
   $env:REQUEST_TIMEOUT_SECONDS = '90'
   $jwt = Read-Host '当前用户 JWT' -AsSecureString
   $env:AGENT_EVAL_BEARER_TOKEN = [pscredential]::new('agent', $jwt).GetNetworkCredential().Password
   $provider = Read-Host '网关配置的供应商 ID'
   $model = Read-Host '网关配置的模型名'
   $dir = 'D:\rubbish\zhongjian\artifacts\flmh-p\agent-evaluation'
   ```

3. 先用跨区类目任务验证整条路径。它同时包含一个独立查询和一个必须等待上游类目的查询。

   ```powershell
   $report = Join-Path $dir ("bound-" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.jsonl')
   & $py -B -m flmh_agent.evaluate_agent --case bound-category --provider $provider --model $model --output $report
   ```

4. 要跑全部样本，使用新的结果文件。只跑其中一组时可加 `--suite planning` 或 `--suite memory`。可重复传 `--case` 选择多个样本。

   ```powershell
   $report = Join-Path $dir ("all-" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.jsonl')
   & $py -B -m flmh_agent.evaluate_agent --provider $provider --model $model --output $report
   $LASTEXITCODE
   Get-Content -LiteralPath $report -Encoding utf8 -Tail 1 | ConvertFrom-Json | ConvertTo-Json -Depth 12
   ```

入口读取当前进程的环境变量。输出文件独占新建，每完成一个样本的一侧就刷新一条 JSONL。当前全集最多发出 **374 次网关请求**，这是运行上限而非预期实际次数。规划组有 16 题，每题 Planner 至多 2 次、ReAct 至多 5 次。记忆组有 12 段、共 131 轮，每轮每侧各请求一次。供应商费用按实际用量结算。

## 样本和评分

| 对照 | 固定输入 | 模型必须做什么 |
| --- | --- | --- |
| Planner / 串行 ReAct | `planning.json` 的 30 件合成商品、16 道题，覆盖 US/CA/ID/GB/TH/VN、多类目价格、三至四市场独立查询、两级类目依赖、价格边界和合法空结果。 | Planner 用 `ModelGatewayPlanner` 生成并校验计划，分层执行后再请求模型给答案。ReAct 每次根据已有观测给出一个查询或最终答案。两边共用生产 `_layered_search`、查询参数、结果格式、四条工具调用上限和最终评分。 |
| 版本化槽位 / 最近历史加截断 | `memory.json` 的 12 段固定对话各有 10–16 轮，共 131 轮、472 个标注字段。覆盖只改制作预算、只改商品限价、跨市场换品、显式撤销、历史淘汰及较长商品摘要/脚本意见。 | 每侧的系统提示和每轮用户消息相同，模型给出本轮更新 `updates` 和当前条件 `constraints`。槽位侧用生产 `update_slots` 合并模型自己提取的更新，只把显式字段及版本带到下一轮。历史侧使用固定的用户原文和助手回复。人工标签仅在评分时读取。 |

历史窗口默认对齐旧版 `take: 12` 的计数单位。旧版先保存本轮用户消息，所以默认选最多 **11 条过去消息**。本轮完整问题单独传入。`--history-unit turn` 改为保留 **12 个完整的先前问答轮次**。两种口径都先取窗口，再保留历史末尾的 Unicode 字符，默认 `--history-chars 2400`。这是对照实验的总历史字符预算，不等同于旧版逐条截断：旧版工具输入最多 800 字符、工具输出最多 1200 字符；本实验以最近消息窗口为主，并用统一总预算限制历史长度。逐轮记录的 `dropped_messages` 和 `dropped_chars` 可定位窗口丢失信息的位置。

规划题按市场核对完整 ID 列表、顺序及实际执行过的查询参数。跨区题还检查上游确实在更早的批次返回所用类目，Planner 的 `category_from_query` 必须绑定到指定查询。只猜中答案会缺少工具证据。工具结果标为 `LOCAL_EVALUATION_FIXTURE`，并发统计反映执行器分批调度，计时与线上商品库性能在本实验范围之外。

多轮题按“评分轮 × 明确标注的字段”计数。当前共标出 472 项字段，每侧分母相同。`region`、`product`、`product_max_price`、`budget_max` 各占一项。数字按数值比较，显式撤销要求模型输出对应键和 `null`。整段对话的 token 合计包括没有评分标签的过渡轮。

## 读结果

首行 `metadata` 含数据文件 SHA-256、样本 ID、历史口径和生成设置。每个 `case` 保存实际提示、模型回复、工具批次及评分。末行 `summary` 给出配对汇总。中断后可读取已经落盘的 `case`，完整结束的文件包含 `summary`。

| 字段 | 口径 |
| --- | --- |
| `model_calls` / `pinned_model_responses` | 前者是客户端发出的模型请求次数，含 HTTP 失败。后者有网关返回且命中指定真实供应商与模型。网关内部重试、切备和供应商账单次数需查网关或供应商日志。 |
| `input_tokens` / `output_tokens` / `total_tokens` | 只读网关 `usage.inputTokens/outputTokens`。总量为两者之和。保留供应商报告的零。缺一项则该项合计和总量写 `null`。逐次 `usage_status` 标出 `complete`、`partial`、`missing` 或 `invalid`。即使模型 JSON 错误，仍保留已收到的真实 usage。 |
| `decision_and_tool_rounds` | 明确按 `model_calls + tool_batches` 计算，合并表示模型决策请求数与工具调度批次数；并行派发的一批算一个工具执行轮次。 |
| `tool_calls` / `tool_batches` / `parallel_batches` | 分别是实际派发的工具调用数、非空批次数、宽度大于 1 的批次数。`tool_trace` 的每个内层数组是一批。被上游依赖阻断的节点不会产生调用。 |
| `matched` / `evaluated` / `expected` | 正确字段数、实际评分的字段数、计划评分的字段数。`retention_rate = matched / expected` 要求所有字段都可评分。执行中断时全量率为 `null`，另给 `unscored` 和已评分部分的 `observed_rate`。 |

模型响应、结构校验或工具执行失败会写入 `error`。该对话余下轮次标为 `skipped_after_error`。完整执行但答错的规划题记录 `correct=false`。解读均值时先看 `failed_ids`、`wrong_answer_ids`、`incomplete_usage_ids` 和样本分母 `cases`。

`paired_ids` 给出计算平均模型调用和工具批次的共同样本。规划组要求两侧均执行完成且答对，记忆组要求两侧均执行完成，约束丢失照实计分。`usage_paired_ids` 进一步要求两侧每次调用都有完整 usage。`paired_token_reduction` 在这些相同样本上算 `1 − 候选侧 token 总和 / 基线侧 token 总和`，基线分别为 ReAct、历史窗口。缺少有效配对或基线为零时写 `null`。汇报这组小样本时可同时引用配对样本 ID、分子分母与失败案例。

退出码 `0` 表示逐臂执行完成且 usage 完整，答题质量见评分字段。`1` 表示已有报告，其中有执行失败或 usage 不完整。`2` 表示参数、数据、路径或配置错误。
