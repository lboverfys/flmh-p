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

Agent 是 MCP client：真正调用商品/内容工具时才连接 `MCP_TOOLS_URL`，初始化后核对工具集，再在同一 MCP 会话内执行独立商品查询；会话由同一异步任务建立和关闭。独立知识/BI 请求不依赖 MCP 可达性。工具桥仅将 Java Gateway REST 转成 MCP，不持有 Agent 逻辑。模型供应商、模型名及厂商密钥在 Java Model Gateway 配置；Python 只使用 `MODEL_GATEWAY_URL` 和服务认证键。生产网关、Java 服务及 Python 两个 Java client 共用 `FLMH_SERVICE_API_KEY` 对应值，调用方以 `X-Service-Key` 发送；Gateway 还要求 `X-Client-Id`。这是服务认证边界，不是终端用户身份或租户授权。

MCP 工具桥仅允许绑定本机回环地址。CLI 和 API 在 Windows 启用 PostgreSQL Checkpointer 时选择 psycopg 兼容的 Selector 事件循环；直接使用其他 ASGI 启动器时，也必须显式配置兼容循环。

请求取消会终止尚在连接的 MCP 握手，并等待同任务关闭会话；重复取消不会打断清理。MCP HTTP/SSE 和 RPC 读取期限统一使用 `REQUEST_TIMEOUT_SECONDS`。Postgres advisory lock 获取结果不确定或解锁失败时丢弃对应连接，防止带会话锁归池；PIT 逐个清理，单个失败仍尝试其余 ID，业务异常与清理异常共同传播。上述取消时序用本机异步替身验证，尚未做真实 Postgres/ES 断网故障注入。

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

启动后在本机浏览器打开 `http://127.0.0.1:8090/` 即可使用中文工作台；它复用同一 Agent API，无需额外前端服务或依赖。页面提供选品与内容、商品历史、商品详情示例，内容报价必须点击“确认并创建任务”后才提交。配置了 `AGENT_API_KEY` 时，在页面“访问设置”填写该 key；只保留在当前页面内存，关闭页面后清空。超时或连接中断后先用保留的运行编号读取状态，避免盲目新建重复请求。

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

指定商品历史无需模型或 BI 数据库配置：

```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.cli `
  --request '查询 camping-lamp-001 的历史趋势'
```

该请求经 LangGraph 的 `read_product_history` 节点调用 MCP `product_history`，返回 Java 持久化的指标快照、采集时间、评分版本及各条来源。默认取最近 30 个版本；MCP 可传 `limit`（1–100）和 `before_version` 翻页，Agent 不自动加载全部历史。`productHistory` 保留原始 nullable 字段，汇总不补值、不计算增长率，也不把采集快照视为日统计。商品槽位修订会失效旧历史并重新读取；未提供明确商品 ID 的请求保留原有检索/BI 行为。

将请求改为“查询 camping-lamp-001 的详情”，会经 MCP `product_detail` 返回 `productDetail`：商品、店铺、卖家、来源类目以及原始图片/详情链接。链接只作为数据保留，未自动访问；未知字段保持 null。商品槽位变化会清空旧详情再读取，失败时可重试对应节点。

查询计划还支持 `category_from_query`：从直接上游查询的首条商品读取类目，再作为下游类目过滤条件。必须同时在 `depends_on` 引用该上游，不得同时填写静态 `category`。上游无结果或类目缺失/非法时，下游查询失败，不放宽成无类目搜索。每条计划最多一次 MCP 查询，计划最多 4 条；同层独立查询并行，缓存同时考虑实际参数和上游结果。现有本地规则不会自动推导这种复杂计划；已提供明确计划示例：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m examples.cross_region_category
```

该示例在 Java 和 MCP 服务已启动时实际查询美国露营灯，再查询加拿大同类商品；使用 `SUPPLIED_EXAMPLE` 标记预先提供的计划，不调用或冒充模型规划。全自动验收脚本也会执行此例。

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
| RAG | BGE-M3 HTTP 批量返回 dense/sparse；Elasticsearch PIT 内并行召回；RRF；BGE-Reranker 批量重排；原版本索引 `_mget` 父块回填；LangGraph 知识问答及内容参考资料节点 | 独立检索 HTTP 返回 503；Agent 知识请求返回 `needs_configuration` 与 TODO |

Postgres Checkpointer 的 schema setup 默认关闭；明确初始化数据库时才使用 `POSTGRES_CHECKPOINT_SETUP=true`。BI 使用独立的 `BI_DATABASE_URI` 和只读数据库账号；不共用 Checkpointer 的写权限。BI HTTP 使用每个 role 的独立 bearer key，角色由服务器 key 映射确定，不信任调用者自报角色头。Agent BI 只在配置 `AGENT_SQL_PRINCIPAL` 时启用对应服务角色。

SQL 校验来源是旧 `biGraph.ts` 和 Prisma schema 的五表：`market_products`、`market_product_daily_stats`、`market_radar_metrics`、`market_canonical_categories`、`market_source_categories`。运行时角色权限只能进一步收缩，不能扩大 allowlist。校验拒绝多语句、非 SELECT、SELECT *、危险函数、越权表/非 public schema、超过 50 的 LIMIT；缺少 LIMIT 时补 20。CTE/JOIN 使用 AST scope 分辨真实表；每个雷达指标分支必须限定 `score_version='v2'`，除非明确请求评分版本比较。Postgres `round(x, precision)` 转为 numeric/decimal，执行事务只读且超时为 5 秒。生成/执行失败会携带错误原因最多修正 3 次。

BGE-M3 HTTP 契约：发送 `{"model":"<配置值>","input":["文本"],"returnDense":true,"returnSparse":true}`；返回 `{"data":[{"denseVector":[...],"sparseVector":{"token":0.1}}]}`。重排发送 model/query/documents/topN，返回 `{"results":[{"index":0,"relevanceScore":0.9}]}`。HTTP URL 是完整 endpoint。Elasticsearch chunk 索引字段使用 `dense_vector`、`sparse_vector`、`text`、`parent_id`，父块以 ID `_mget` 获取。填写服务地址前需让部署实现匹配这些明确契约。

知识入库使用 UTF-8 文本，每个文件最多 1 MiB；父块 3000 字符，子块最多 800 字符、步长 700，每批最多 32 个子块一次嵌入并批量写 ES。`ELASTICSEARCH_INDEX` 是知识库名称前缀，只允许 1–80 个小写英文字母、数字、连字符或下划线，首字符必须为字母或数字。每个文档版本写入独立的 `<前缀>-doc-<hash>` 物理索引；`<前缀>-active-<文档hash>` 别名只指向该文档的一个已发布版本。首次建立的空 anchor 索引用于原子发布的基线，不含检索文本。旧的单索引数据不会自动迁移或被此路径查询，需要按新版本重新入库。

所有批次成功后，入库使用 ES write block API 封存索引并等待正在进行的写入结束，刷新后核对父块和子块数量，再通过一次别名 remove/add 发布。清单保存内容指纹、来源、模型及首次入库时的发布基线；同版本不同内容/模型被拒绝。别名切换要求旧基线仍存在，其他版本已抢先发布时返回冲突，不能把旧重试静默回滚成当前版本。部分失败的 staging 索引不会被检索；使用相同输入重试可补写，已封存但发布响应丢失时重新核实别名并恢复发布；当前已发布同版本直接返回 `ALREADY_PUBLISHED`，不会重新计算向量。封存后计数不符需使用新版本排查重建，不自动解封或删除数据。

每次检索通过已发布别名集合打开 PIT，将两路召回固定在同一时刻，并在成功或失败时关闭 PIT。父块按命中结果的物理索引批量读取；旧索引不自动删除，因此发布中途切换不会让父块落到新版本。不同文档按各自发布事务生效，当前没有跨全库版本事务。当前每个文档版本对应一个物理索引，生产启用前应确定文档规模、分片配额和旧版本保留/清理策略；真实集群的并发别名切换、write block 和 PIT 行为仍待联调，不把 HTTP 合同测试当作已完成生产验证。

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

测试覆盖确认闸门、预算、状态刷新、报价幂等、run_id 冲突、并发查询/同 run 协调、失败节点与失败查询重试、独立槽位版本、修订并发控制、旧报价确认拒绝、DAG 类目绑定/缓存失效/并行校验、商品历史及详情计划/工具/失败重试、模型/MCP/BGE/ES HTTP 合同、批量入库及发布隔离、RRF/父块、Text2SQL AST/权限、连接池并发初始化、MCP 会话生命周期与重复取消、advisory lock/PIT 清理失败、Windows PostgreSQL 事件循环及 API 输入。2026-11-17 使用上述 Python 3.12.14 环境运行 110 项测试全部通过，`pip check` 通过；日志为 `D:\rubbish\zhongjian\temp\flmh\agent-arch\query-binding-full-tests.log`。此前入库 CLI `--help` 也已通过。已检查 PostgreSQL/Langfuse 可选库接口；这不等于真实服务联调。当前未在真实 PostgreSQL、Langfuse、BGE 服务或 Elasticsearch 上联调；没有运行性能基准，不以简历历史数字冒充本次数据。

## 待配置与未验证项

`.env.example` 中所有服务地址/凭据空项由部署时填写。明天需决定 PostgreSQL Checkpointer 与只读 BI 库、Langfuse、BGE-M3/BGE-Reranker 服务、ES chunk 索引与认证方式。真实模型供应商与模型参数由 Java Model Gateway 维护。真实外部 adapter 联调、数据库权限/索引执行计划验证、跨主机负载和性能基准尚未运行。不得把本地 adapter 或 HTTP contract 测试当作生产数据、实际召回效果或完整生产部署验收。

当前 Agent API 的 bearer key 是服务边界认证，尚未接入最终用户身份系统及租户隔离；MCP 仅绑定本机。商品历史工具已接通；旧系统日统计、完整增长评分和排名趋势仍未迁移。旧项目只提供关联达人数量等商品指标，未据此虚构独立达人目录。上述限制不影响本机商品 → 报价 → 确认 → 内容任务链路。
