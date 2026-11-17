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

Agent 是 MCP client：真正调用商品/内容工具时才连接 `MCP_TOOLS_URL`，初始化后核对工具集，再在同一 MCP 会话内执行独立商品查询；会话由同一异步任务建立和关闭。独立知识/BI 请求不依赖 MCP 可达性。工具桥仅将 Java Gateway REST 转成 MCP，不持有 Agent 逻辑。模型供应商、模型名及厂商密钥在 Java Model Gateway 配置；Python 只使用 `MODEL_GATEWAY_URL` 和服务认证键。生产网关、Java 服务及 Python 两个 Java client 共用 `FLMH_SERVICE_API_KEY` 对应值，调用方以 `X-Service-Key` 发送；所有公开Java请求另传当前用户JWT，服务key不能代替用户登录。Agent HTTP和MCP逐请求向identity-service核实当前用户；生产拒绝Mock目录。

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

启动后在本机浏览器打开 `http://127.0.0.1:8090/`，先登录再使用中文工作台；它复用同一 Agent API，无需额外前端服务或依赖。页面提供选品与内容、商品历史、商品详情示例，内容报价必须点击“确认并创建任务”后才提交。本机Mock账号为 `alice@demo.flmh.invalid` / `DemoAlice2026!`、`bob@demo.flmh.invalid` / `DemoBob2026!`；管理员为 `admin@demo.flmh.invalid` / `DemoAdmin2026!`。用户JWT只在页面内存中，换用户会清空结果与运行编号。配置了 `AGENT_API_KEY` 时还需在访问设置填写独立的Agent访问凭据，使用 `X-Agent-Key` 发送；Java服务key不会进入浏览器。超时或连接中断后先用保留的运行编号读取状态，避免盲目新建重复请求。

或先登录并把JWT仅保存到当前PowerShell进程，再使用 CLI/示例：

```powershell
$login = Invoke-RestMethod -Method Post 'http://127.0.0.1:8080/api/v1/identity/login' `
  -ContentType 'application/json' -Body '{"email":"alice@demo.flmh.invalid","password":"DemoAlice2026!"}'
$env:FLMH_USER_TOKEN = $login.token
$headers = @{Authorization = "Bearer $($login.token)"}
# 若配置AGENT_API_KEY，再添加 $headers['X-Agent-Key'] = $env:AGENT_API_KEY
```

CLI在每次运行前核实用户JWT；identity-service重启后local令牌失效，需重新登录。



```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.cli `
  --request '请帮我在美国查露营灯并写口播脚本，预算不超过 30'
```

CLI 展示报价后，输入 `y` 才确认并创建任务。`--confirm` 是调用者的明确确认，不用于伪造用户许可。

HTTP 示例：

```powershell
$run = Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/agent/runs' `
  -Headers $headers -ContentType 'application/json' `
  -Body '{"run_id":"local-request-001","user_request":"请帮我在美国查露营灯并写口播脚本，预算不超过 30"}'
$run.confirmationRequest
Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/agent/runs/local-request-001/confirmation' `
  -Headers $headers -ContentType 'application/json' -Body (@{confirmed=$true; quote_id=$run.confirmationRequest.quoteId} | ConvertTo-Json)
Invoke-RestMethod 'http://127.0.0.1:8090/v1/agent/runs/local-request-001' -Headers $headers
```

指定商品历史无需模型或 BI 数据库配置：

```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.cli `
  --request '查询 camping-lamp-001 的历史趋势'
```

该请求经 LangGraph 的 `read_product_history` 节点调用 MCP `product_history`，返回 Java 持久化的指标快照、采集时间、评分版本及各条来源。默认取最近 30 个版本；MCP 可传 `limit`（1–100）和 `before_version` 翻页，Agent 不自动加载全部历史。`productHistory` 保留原始 nullable 字段；摘要比较采集时间最新的两个不同快照，仅在来源相同且数值完整时计算销量、关联达人数的差值及相对变化，并显示实际时间间隔和两条引用。来源缺失/变化、样本或字段不足、零基线均明确标注；逐快照未保存币种，因此不计算金额变化。不把这些快照差值外推成日增长、7 日增长或旧版综合趋势评分。商品槽位修订会失效旧历史并重新读取；未提供明确商品 ID 的请求保留原有检索/BI 行为。

将请求改为“查询 camping-lamp-001 的详情”，会经 MCP `product_detail` 返回 `productDetail`：商品、店铺、卖家、来源类目以及原始图片/详情链接。链接只作为数据保留，未自动访问；未知字段保持 null。商品槽位变化会清空旧详情再读取，失败时可重试对应节点。

查询计划还支持 `category_from_query`：从直接上游查询的首条商品读取类目，再作为下游类目过滤条件。必须同时在 `depends_on` 引用该上游，不得同时填写静态 `category`。上游无结果或类目缺失/非法时，下游查询失败，不放宽成无类目搜索。每条计划最多一次 MCP 查询，计划最多 4 条；同层独立查询并行，缓存同时考虑实际参数和上游结果。现有本地规则不会自动推导这种复杂计划；已提供明确计划示例：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m examples.cross_region_category
```

该示例在 Java 和 MCP 服务已启动时实际查询美国露营灯，再查询加拿大同类商品；使用 `SUPPLIED_EXAMPLE` 标记预先提供的计划，不调用或冒充模型规划。全自动验收脚本也会执行此例。

同一用户重复 `run_id` 和完全相同输入回放已有状态；不同用户的同名运行独立。服务端按用户ID与run_id派生检查点和锁键，用户不能通过工具参数自报owner。历史无归属检查点保留且不会被新用户读取。请求体发生变化返回 409。同一 run 的并发启动、确认、修订和刷新有运行协调锁；memory 模式在单进程内生效，Postgres 模式使用 PostgreSQL advisory lock。确认请求必须带当前报价 ID，避免修订后旧页面误确认新报价。任务幂等键包含报价 ID；缺少有效预算返回 `needs_input`，不猜金额、不请求报价。异步任务返回 `processing`，GET 运行记录刷新 Java 任务状态，直到已知终态。

确认生产前可以显式修订运行记录；`expected_revision` 防止并发覆盖，成功后 revision 加 1。商品、地区、预算有独立 `slotVersions`，结论保留 `conclusionDependencies`，查询按实际参数和上游结果指纹缓存；未受影响的查询复用，其余重新执行。修订将旧报价确认失效，已确认或提交生产的运行禁止改写，应创建新 run。

```powershell
Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/agent/runs/local-request-001/revision' `
  -Headers $headers -ContentType 'application/json' -Body '{"expected_revision":0,"slot_updates":{"region":"CA","budget_max":40}}'
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

Postgres Checkpointer 的 schema setup 默认关闭；明确初始化数据库时才使用 `POSTGRES_CHECKPOINT_SETUP=true`。BI 使用独立的 `BI_DATABASE_URI` 和只读数据库账号；不共用 Checkpointer 的写权限。BI HTTP和Agent BI都使用核实后的用户角色，`BI_SQL_ROLE_TABLES_JSON`只配置 `USER`/`ADMIN`/`SUPER_ADMIN` 的表权限。角色头、旧共享role token和 `AGENT_SQL_PRINCIPAL` 不能提升权限。

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

测试覆盖确认闸门、预算/币种、幂等、查询DAG、槽位失效、日统计、媒体未知结果恢复、MCP确认、HTTP协议、RAG、Text2SQL以及账号权限和工作台。2026-11-17本轮Python3.12.14全量176项通过、Node工作台23项通过，日志在`D:\rubbish\zhongjian\temp\flmh\accounts-audit-20261117`；pip check通过。本轮最终跨进程58/58通过，包含原Agent链路及账号创建/资料/改密/禁用即时生效/当前角色/积分/会员/重启/删除和采集模板。Chrome实际完成管理员开户、编辑、积分、会员、普通用户资料改密、新密码重登、退出清空和删除；临时页面与服务已收尾。此前媒体和双用户隔离浏览器证据保留在Java交付记录的历史产物中。真实PostgreSQL、Langfuse、BGE及ES未联调，没有性能基准或召回效果指标。完整证据见[Java交付记录](../flmh-j/docs/delivery.md)。

## 待配置与未验证项

登录后可展开“账号资料与用户管理”：自己资料与旧密码校验改密使用 `/v1/identity/profile`、`/v1/identity/password`；管理员通过 `/v1/identity/users` 及其子路径完成分页查询、创建、角色/状态/密码修改、积分与会员设置、删除。管理操作走受限HTTP入口，不暴露给LLM或MCP；Java再次核实当前目录角色，Python只转发服务端配置的内部凭据和当前用户JWT，响应按安全字段白名单输出。

账号数据由Java持久化到本地H2，重启不覆盖已有资料或密码；JWT仍随身份进程重启失效。工作台积分请求结果未知后清空原调整并锁定目标，重新读取并选择用户后才允许下一次操作；**当前余额不能判断上一笔是否成功，积分API没有服务端幂等键**。页面离开/退出清空账号列表、资料和密码；请求不会自动重发。

`.env.example` 中所有服务地址/凭据空项由部署时填写。明天需决定 PostgreSQL Checkpointer 与只读 BI 库、Langfuse、BGE-M3/BGE-Reranker 服务、ES chunk 索引与认证方式。真实模型供应商与模型参数由 Java Model Gateway 维护。真实外部 adapter 联调、数据库权限/索引执行计划验证、跨主机负载和性能基准尚未运行。不得把本地 adapter 或 HTTP contract 测试当作生产数据、实际召回效果或完整生产部署验收。

Agent API与MCP使用用户JWT逐请求核实身份，检查点与锁按用户隔离；可选X-Agent-Key仍只是额外服务访问边界。MCP仅绑定本机。商品历史和日统计工具均已接通，资料与管理员用户管理已接入同源工作台；完整旧前端、密码找回及工作流编辑仍未迁移。旧项目只提供关联达人数量等商品指标，未据此虚构独立达人目录。

## 图片与视频工作流状态验证

`application/media_workflow.py` 提供独立 LangGraph 用例，经 `MediaWorkflowPort` → MCP → Java 内容服务执行固定的图片、视频两节点状态流程。请求必须显式给出布尔值 `mock_execution_confirmed=true`；节点依赖、定义/输入快照、运行状态、幂等回放和取消都持久化到 Java 内容数据库。终态为 `MOCKED`，`artifacts=[]`、`artifactCreated=false`、`outputUrl=null`，不生成文件，也不产生外部供应商费用。

本地服务启动后可执行下面的验收示例；它显式确认零成本状态模拟，并核对未确认拒绝、幂等冲突及取消后禁止执行：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m examples.media_workflow
```

上述旧状态模拟入口保留作为独立示例。自然语言工作台使用下面的媒体生产流程，不调用该 simulate 接口。

## 媒体生产主流程

输入 `查美国旅行杯并制作图片和视频，预算10`，规则计划选择 `product_media`，经 LangGraph → MCP → Java `/api/v1/content/media-productions` 获取报价。初版主流程明确展示一张图片再生成 6 秒、9:16 视频；确认前不会提交供应商。商品限价与内容预算分别处理；明确使用“元/CNY”的预算不能用于 USD 报价，也不会自动换汇。未指定币种时按报价显示的币种确认。超过 2000 字符的媒体提示词（含商品和知识资料）返回可修订错误，不静默截断。

共享确认入口仍为 `POST /v1/agent/runs/{runId}/confirmation`，必须带当前报价 ID 与真正的布尔值。即使本地零元报价也要求确认。确认仅幂等创建 `QUEUED` 运行，后续操作均显式触发：

- `GET /v1/agent/runs/{runId}`：只读取 Java 已保存状态，可恢复上次状态读取中断。
- `POST /v1/agent/runs/{runId}/media/advance`：推进一次提交、存储或查询阶段，不携带正文。
- `POST /v1/agent/runs/{runId}/media/verify`：只核实 `SUBMISSION_UNKNOWN`，不重新提交付费请求。

工作台展示逐节点状态、来源、报价规格和产物记录，地址仅作文本。`MOCK_ONLY/MOCKED` 明确没有图片或视频文件。推进响应丢失后先读取状态；未知结果禁止直接推进，只有核实入口。Java 提供 OpenAI Images/Responses、Gemini generateContent、旧项目 TOAPIS xai/volcengine 视频协议及 OSS HTTP 适配，所有端点、模型、价格和凭据在 Java `.env.example` 留空；本任务只运行本机合同测试。视频转存 OSS、工作流编辑和供应商丢失任务 ID 后的真实对账仍未完成。

MCP还提供`media_production_revise`：先以`origin_run_id`申请失败运行的新报价，经独立确认后传原运行、新报价和幂等键提交。Java只允许明确FAILED的原运行，禁止未知结果重投；未变化的成功图片可复用。该分支完成接口合同与Java状态机测试，未接工作台自然语言自动修订，也未作真实供应商端到端验证。

## 来源日统计与评分

输入 `查询 camping-lamp-001 的日统计`，Agent 先读取商品来源，再按该来源调用 MCP `product_daily_statistics`；每次固定两次工具调用，日统计最多返回 30 条，MCP 可传 `limit`（1–100）和 `before_date`。响应 `productDaily` 保留评分版本、观测、趋势、限制说明与日期游标，不随条数增加请求或自动翻完全部历史。商品槽位更新会失效旧日统计。

日统计由 Java 内部重算用例生成，Agent 只读；没有已保存统计时明确返回空，不把快照差值冒充日报。`radar-v2-exact-date` 延续旧版分项评分，但 7/30 天增长要求精确日期基线；缺失基线保持 null。金额和类目聚合按来源、币种隔离，原始滚动 7 天销量不会改称当天销量。真实数据库查询计划及生产规模未验证。
