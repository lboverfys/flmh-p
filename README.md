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

Agent 是 MCP client：它连接 `MCP_TOOLS_URL`，初始化后核对工具集，再在同一 MCP 会话内执行独立商品查询。工具桥仅将 Java Gateway REST 转成 MCP，不持有 Agent 逻辑。模型供应商、模型名及厂商密钥在 Java Model Gateway 配置；Python 只使用 `MODEL_GATEWAY_URL` 和服务认证键。生产网关、Java 服务及 Python 两个 Java client 共用 `FLMH_SERVICE_API_KEY` 对应值，调用方以 `X-Service-Key` 发送；Gateway 还要求 `X-Client-Id`。这是服务认证边界，不是终端用户身份或租户授权。

MCP 工具桥仅允许绑定本机回环地址。CLI 和 API 在 Windows 启用 PostgreSQL Checkpointer 时选择 psycopg 兼容的 Selector 事件循环；直接使用其他 ASGI 启动器时，也必须显式配置兼容循环。

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
  -ContentType 'application/json' -Body (@{confirmed=$true; quote_id=$run.confirmationRequest.quoteId} | ConvertTo-Json)
Invoke-RestMethod 'http://127.0.0.1:8090/v1/agent/runs/local-request-001'
```

重复 `run_id` 和完全相同输入回放已有状态；请求体发生变化返回 409。同一 run 的并发启动、确认、修订和刷新有运行协调锁；memory 模式在单进程内生效，Postgres 模式使用 PostgreSQL advisory lock。确认请求必须带当前报价 ID，避免修订后旧页面误确认新报价。任务幂等键包含报价 ID；缺少有效预算返回 `needs_input`，不猜金额、不请求报价。异步任务返回 `processing`，GET 运行记录刷新 Java 任务状态，直到已知终态。

确认生产前可以显式修订运行记录；`expected_revision` 防止并发覆盖，成功后 revision 加 1。商品、地区、预算有独立 `slotVersions`，结论保留 `conclusionDependencies`，查询按实际参数和上游结果指纹缓存；未受影响的查询复用，其余重新执行。修订将旧报价确认失效，已确认或提交生产的运行禁止改写，应创建新 run。

```powershell
Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/agent/runs/local-request-001/revision' `
  -ContentType 'application/json' -Body '{"expected_revision":0,"slot_updates":{"region":"CA","budget_max":40}}'
```

计划最多 4 条商品查询，可选 `query_id` 与 `depends_on`。校验拒绝未知引用、重复 ID 和环；按依赖层并行执行，无依赖查询同批运行。失败后 `POST /v1/agent/runs/{run_id}/retry` 继续 LangGraph checkpoint 的失败节点；并行查询部分失败时保存成功结果，只重试失败查询及其后继。报价等待确认时不能通过 retry 绕过确认。

## 外部适配器

| 能力 | 可配置实现 | 未配置状态 |
| --- | --- | --- |
| 规划 / Text2SQL | Java Model Gateway `/api/v1/models/completions`，请求带 purpose/messages/temperature/maxTokens/responseFormat | `rules` 可运行本地商品链路；BI 必须配置 |
| 内容生产 | 确认后经 MCP 创建 Java 内容任务，provider 由内容服务配置 | 输入摘要只整理用户需求，不伪造生成结果 |
| 商品 / 内容工具 | MCP Streamable HTTP；桥接 Java Gateway API | 工具服务不可达时报错，不伪造商品或任务 |
| Checkpoint | `langgraph-checkpoint-postgres` 的 AsyncPostgresSaver | 默认 InMemorySaver；进程退出后状态消失 |
| 观测 | Langfuse v3 CallbackHandler；按配置启用并在退出时 flush/shutdown | 无 key 时禁用 |
| Text2SQL | Java Model Gateway 生成，SQLGlot AST 校验，asyncpg read-only transaction | 空权限或空连接默认关闭 |
| RAG | BGE-M3 HTTP 批量返回 dense/sparse；Elasticsearch 并行召回；RRF；BGE-Reranker 批量重排；`_mget` 父块回填；LangGraph 知识问答及内容参考资料节点 | 独立检索 HTTP 返回 503；Agent 知识请求返回 `needs_configuration` 与 TODO |

Postgres Checkpointer 的 schema setup 默认关闭；明确初始化数据库时才使用 `POSTGRES_CHECKPOINT_SETUP=true`。BI 使用独立的 `BI_DATABASE_URI` 和只读数据库账号；不共用 Checkpointer 的写权限。BI HTTP 使用每个 role 的独立 bearer key，角色由服务器 key 映射确定，不信任调用者自报角色头。Agent BI 只在配置 `AGENT_SQL_PRINCIPAL` 时启用对应服务角色。

SQL 校验来源是旧 `biGraph.ts` 和 Prisma schema 的五表：`market_products`、`market_product_daily_stats`、`market_radar_metrics`、`market_canonical_categories`、`market_source_categories`。运行时角色权限只能进一步收缩，不能扩大 allowlist。校验拒绝多语句、非 SELECT、SELECT *、危险函数、越权表/非 public schema、超过 50 的 LIMIT；缺少 LIMIT 时补 20。CTE/JOIN 使用 AST scope 分辨真实表；每个雷达指标分支必须限定 `score_version='v2'`，除非明确请求评分版本比较。Postgres `round(x, precision)` 转为 numeric/decimal，执行事务只读且超时为 5 秒。生成/执行失败会携带错误原因最多修正 3 次。

BGE-M3 HTTP 契约：发送 `{"model":"<配置值>","input":["文本"],"returnDense":true,"returnSparse":true}`；返回 `{"data":[{"denseVector":[...],"sparseVector":{"token":0.1}}]}`。重排发送 model/query/documents/topN，返回 `{"results":[{"index":0,"relevanceScore":0.9}]}`。HTTP URL 是完整 endpoint。Elasticsearch chunk 索引字段使用 `dense_vector`、`sparse_vector`、`text`、`parent_id`，父块以 ID `_mget` 获取。填写服务地址前需让部署实现匹配这些明确契约。

知识入库使用 UTF-8 文本，每个文件最多 1 MiB；父块 3000 字符，子块最多 800 字符、步长 700，每批最多 32 个子块一次嵌入并批量写 ES。固定文档 ID、版本和来源生成稳定块 ID；同版本不同正文或来源会拒绝，失败可使用原输入重试。新版本并存，生产发布应使用独立索引/别名控制生效版本，当前没有自动清理旧版本或全库重建。

配置目标服务并明确授权写入后使用（本任务未执行真实入库）：

```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.ingest `
  --file 'D:\path\manual.txt' --document-id 'product-manual' --version 'v1' --source-ref 'local:product-manual'
```

Agent 请求“从知识库查询采集说明”进入检索路径并输出带来源的资料；模型规划也可使用 `knowledge_query` 为商品内容请求补充事实。检索资料被明确当作引用数据，不能直接授权确认或调用工具。不填写模型配置时不生成模拟向量或宣称任何召回效果。

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

测试覆盖确认闸门、预算、状态刷新、报价幂等、run_id 冲突、并发查询/同 run 协调、失败节点与失败查询重试、独立槽位版本、修订并发控制、旧报价确认拒绝、DAG 依赖校验、模型/MCP/BGE/ES HTTP 合同、批量入库、RRF/父块、Text2SQL AST/权限、Windows PostgreSQL 事件循环及 API 输入。2026-11-17 使用上述 Python 3.12.14 环境运行 55 项测试全部通过，入库 CLI `--help` 和 `pip check` 通过。已检查 PostgreSQL/Langfuse 可选库接口；这不等于真实服务联调。当前未在真实 PostgreSQL、Langfuse、BGE 服务或 Elasticsearch 上联调；没有运行性能基准，不以简历历史数字冒充本次数据。

## 待配置与未验证项

`.env.example` 中所有服务地址/凭据空项由部署时填写。明天需决定 PostgreSQL Checkpointer 与只读 BI 库、Langfuse、BGE-M3/BGE-Reranker 服务、ES chunk 索引与认证方式。真实模型供应商与模型参数由 Java Model Gateway 维护。真实外部 adapter 联调、数据库权限/索引执行计划验证、跨主机负载和性能基准尚未运行。不得把本地 adapter 或 HTTP contract 测试当作生产数据、实际召回效果或完整生产部署验收。

当前 Agent API 的 bearer key 是服务边界认证，尚未接入最终用户身份系统及租户隔离；MCP 仅绑定本机。达人和独立趋势工具尚未有对应 Java 读服务契约，当前不生成虚构数据。上述限制不影响本机商品 → 报价 → 确认 → 内容任务链路。
