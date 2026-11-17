# FLMH Agent

此工程以旧项目 `flmh/server/src/modules/product-radar` 的选品、BI 和内容请求行为，以及正式简历中的 LangGraph、MCP、PostgreSQL Checkpointer、Langfuse、Text2SQL、BGE-M3/BGE-Reranker 和 Elasticsearch 为依据重构。旧项目和正式 PDF 保持只读。Python 负责规划、状态编排和工具调用；商品、报价、任务状态和模型供应商路由归 Java 服务。

## 结构与依赖方向

```text
flmh_agent/
  domain/              # 计划、输入、状态、槽位和幂等规则
  ports/               # 规划、内容、MCP工具、检索、观测和运行协调端口
  application/         # LangGraph 工作流、Agent use case、Text2SQL 与 RAG pipeline
  adapters/
    inbound/           # MCP 工具桥：Java REST → MCP tools
    outbound/          # MCP client、Java Model Gateway、Postgres、Langfuse、BGE、ES
  api.py               # ASGI HTTP 应用组合与输入校验
  cli.py               # 交互式 CLI
  config.py            # .env / 环境变量 → 只读 Settings
tests/                 # 领域、工作流、安全与适配器合同测试
```

应用逻辑只依赖端口。生产适配器按配置选择；未配置的外部能力返回明确的 disabled/configuration 错误。`rules` 是明示的本地规划适配器，输出来源为 `RULES`，不冒充真实模型输出。报价前只整理用户输入和商品信息成 `INPUT_REQUEST` 摘要，不调用付费内容生成；明确确认后才通过 MCP 提交 Java 内容任务，由内容服务执行真实或本地 provider。`APP_ENV=production` 必须启用模型网关规划、PostgreSQL Checkpointer 和 API key。

Agent 是 MCP client：它连接 `MCP_TOOLS_URL`，初始化后核对工具集，再在同一 MCP 会话内执行独立商品查询。工具桥仅将 Java Gateway REST 转成 MCP，不持有 Agent 逻辑。模型供应商、模型名及厂商密钥在 Java Model Gateway 配置；Python 只使用 `MODEL_GATEWAY_URL` 和可选服务认证键。

## 本机启动

本机已验证 Python 为 `D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe`（3.12.14）。包缓存放在 `D:\rubbish\zhongjian\caches\python\flmh`，临时文件放在 `D:\rubbish\zhongjian\temp\flmh\agent-arch`。不需要下载任何模型权重。

先按 Java 工程说明启动 Gateway 和服务。复制 `.env.example` 为 `.env` 后按需填写，已有 `.env` 不要覆盖；环境变量优先于 `.env`。默认值适用于本地适配器。

窗口一：启动 MCP 工具桥。

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.adapters.inbound.mcp_tools
```

窗口二：启动 Agent API，监听 `127.0.0.1:8090`。

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.api
```

或直接使用 CLI：

```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.cli `
  --request '请帮我在美国查露营灯并写口播脚本，预算不超过 30'
```

CLI 展示报价后，输入 `y` 才确认并创建任务。`--confirm` 是调用者的明确确认，不用于伪造用户许可。

HTTP 示例：

```powershell
$run = Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/agent/runs' `
  -ContentType 'application/json' `
  -Body '{"run_id":"local-request-001","user_request":"请帮我在美国查露营灯并写口播脚本，预算不超过 30"}'
$run.confirmationRequest
Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/agent/runs/local-request-001/confirmation' `
  -ContentType 'application/json' -Body '{"confirmed":true}'
Invoke-RestMethod 'http://127.0.0.1:8090/v1/agent/runs/local-request-001'
```

重复 `run_id` 和完全相同输入回放已有状态；请求体发生变化返回 409。同一 run 的并发启动/确认有运行协调锁；memory 模式在单进程内生效，Postgres 模式使用 PostgreSQL advisory lock。中断状态保留报价、槽位版本和输入快照；改变输入必须使用新 run。失败后 `POST /v1/agent/runs/{run_id}/retry` 通过 LangGraph checkpoint 继续失败节点，不重跑已成功节点；报价等待确认时不能通过 retry 绕过确认。

## 外部适配器

| 能力 | 可配置实现 | 未配置状态 |
| --- | --- | --- |
| 规划 / Text2SQL | Java Model Gateway `/api/v1/models/completions`，请求带 purpose/messages/temperature/maxTokens/responseFormat | `rules` 可运行本地商品链路；BI 必须配置 |
| 内容生产 | 确认后经 MCP 创建 Java 内容任务，provider 由内容服务配置 | 输入摘要只整理用户需求，不伪造生成结果 |
| 商品 / 内容工具 | MCP Streamable HTTP；桥接 Java Gateway API | 工具服务不可达时报错，不伪造商品或任务 |
| Checkpoint | `langgraph-checkpoint-postgres` 的 AsyncPostgresSaver | 默认 InMemorySaver；进程退出后状态消失 |
| 观测 | Langfuse v3 CallbackHandler；按配置启用并在退出时 flush/shutdown | 无 key 时禁用 |
| Text2SQL | Java Model Gateway 生成，SQLGlot AST 校验，asyncpg read-only transaction | 空权限或空连接默认关闭 |
| RAG | BGE-M3 HTTP 同时返回 dense/sparse；Elasticsearch 并行召回；RRF；BGE-Reranker HTTP 批量重排；`_mget` 批量父块回填 | 无 embedding backend 时 HTTP 返回 503 |

Postgres Checkpointer 的 schema setup 默认关闭；明确初始化数据库时才使用 `POSTGRES_CHECKPOINT_SETUP=true`。BI 使用独立的 `BI_DATABASE_URI` 和只读数据库账号；不共用 Checkpointer 的写权限。BI HTTP 使用每个 role 的独立 bearer key，角色由服务器 key 映射确定，不信任调用者自报角色头。Agent BI 只在配置 `AGENT_SQL_PRINCIPAL` 时启用对应服务角色。

SQL 校验来源是旧 `biGraph.ts` 和 Prisma schema 的五表：`market_products`、`market_product_daily_stats`、`market_radar_metrics`、`market_canonical_categories`、`market_source_categories`。运行时角色权限只能进一步收缩，不能扩大 allowlist。校验拒绝多语句、非 SELECT、SELECT *、危险函数、越权表/非 public schema、超过 50 的 LIMIT；缺少 LIMIT 时补 20。CTE/JOIN 使用 AST scope 分辨真实表；每个雷达指标分支必须限定 `score_version='v2'`，除非明确请求评分版本比较。Postgres `round(x, precision)` 转为 numeric/decimal，执行事务只读且超时为 5 秒。生成/执行失败会携带错误原因最多修正 3 次。

BGE-M3 HTTP 契约：发送 `{"model":"<配置值>","input":["文本"],"returnDense":true,"returnSparse":true}`；返回 `{"data":[{"denseVector":[...],"sparseVector":{"token":0.1}}]}`。重排发送 model/query/documents/topN，返回 `{"results":[{"index":0,"relevanceScore":0.9}]}`。HTTP URL 是完整 endpoint。Elasticsearch chunk 索引字段使用 `dense_vector`、`sparse_vector`、`text`、`parent_id`，父块以 ID `_mget` 获取。填写服务地址前需让部署实现匹配这些明确契约。

## 依赖与验证

核心依赖在 `pyproject.toml`，`requirements.txt` 引用项目。可选 `postgres`、`observability` extras 只在启用相关适配器时需要；未安装也可运行本地链路。安装前按会话规则检查现有环境与目录，示例：

```powershell
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\agent-arch'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = 'D:\rubbish\zhongjian\caches\python\flmh'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m pip install -e .
# 接入 PostgreSQL / Langfuse 时分别安装 -e '.[postgres]' / -e '.[observability]'
```

运行测试：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m unittest discover -s tests -v
```

测试覆盖确认闸门、预算、状态读取、幂等键、run_id 冲突、并发查询/同 run 协调、失败节点重试、槽位版本失效、模型/MCP/BGE/ES adapter HTTP 合同、RRF/父块、Text2SQL AST/权限，以及 API 输入与配置错误。2026-11-17 使用上述 Python 3.12.14 环境运行 38 项测试全部通过；项目 editable 安装、CLI `--help` 和 `pip check` 通过。当前未在真实 PostgreSQL、Langfuse、BGE 服务或 Elasticsearch 上联调；没有运行性能基准，不以简历历史数字冒充本次数据。

## 待配置与未验证项

`.env.example` 中所有服务地址/凭据空项由部署时填写。明天需决定 PostgreSQL Checkpointer 与只读 BI 库、Langfuse、BGE-M3/BGE-Reranker 服务、ES chunk 索引与认证方式。真实模型供应商与模型参数由 Java Model Gateway 维护。真实外部 adapter 联调、数据库权限/索引执行计划验证、跨主机负载和性能基准尚未运行。不得把本地 adapter 或 HTTP contract 测试当作生产数据、实际召回效果或完整生产部署验收。
