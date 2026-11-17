# FLMH Agent

此工程以旧项目 `flmh/server/src/modules/product-radar` 的选品、BI 和内容请求行为，以及正式简历中的 LangGraph、MCP、PostgreSQL Checkpointer、Langfuse、Text2SQL、BGE-M3/BGE-Reranker 和 Elasticsearch 为依据重构。旧项目和正式 PDF 保持只读。Python 负责规划、状态编排和工具调用；商品、报价、任务状态和模型供应商路由归 Java 服务。

## 结构与依赖方向

```text
flmh_agent/
  domain/              # 计划、输入、状态、槽位和幂等规则
  ports/               # 规划、内容、MCP工具、检索、观测和运行协调端口
  application/         # LangGraph 工作流、Agent use case、Text2SQL 与 RAG pipeline
  adapters/
    inbound/           # MCP 工具桥、工作台与同源商品/账号/作品/素材/工作流/会话 HTTP 入口
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

本机已验证 Python 为 `D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe`（3.12.14）。包缓存放在 `D:\rubbish\zhongjian\caches\python\flmh`，本轮临时与验收目录为 `D:\rubbish\zhongjian\temp\flmh\enterprise-20261117\v12`；Java构建目录为 `D:\rubbish\zhongjian\build\flmh\enterprise-20261117\v12`。不需要下载任何模型权重，不把依赖、缓存或临时数据写入C盘。

最简整链启动使用 [Java README](../flmh-j/README.md#本机启动) 的 `verify-local.ps1` 命令，它会启动六个Java服务、MCP和Agent，完成验证后自动收尾；手动体验时显式追加 `-KeepAlive`，结束后在原终端输入 `stop`。该脚本已经启动Python服务时，不再执行下面两组命令。

分开启动时，先按Java说明启动Gateway和服务，并给MCP/Agent传入同一任务服务密钥对应的 `JAVA_GATEWAY_API_KEY`/`MODEL_GATEWAY_API_KEY`；服务密钥不能代替用户JWT。需要自定义配置时再复制 `.env.example` 为 `.env`，已有 `.env` 不覆盖；环境变量优先于 `.env`。模型和外部组件空配置保持TODO，本地适配器不需要真实模型凭据。

窗口一：启动 MCP 工具桥。

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\enterprise-20261117\v12'
$env:TMP = $env:TEMP
if (!(Test-Path -LiteralPath $env:TEMP)) { New-Item -ItemType Directory -Path $env:TEMP | Out-Null }
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.adapters.inbound.mcp_tools
```

窗口二：启动 Agent API，监听 `127.0.0.1:8090`。

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\enterprise-20261117\v12'
$env:TMP = $env:TEMP
if (!(Test-Path -LiteralPath $env:TEMP)) { New-Item -ItemType Directory -Path $env:TEMP | Out-Null }
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.api
```

启动后在本机浏览器打开 `http://127.0.0.1:8090/`，先登录再使用中文工作台；它复用同一 Agent API，无需额外前端服务或依赖。页面提供选品与内容、商品目录筛选分页、商品历史/详情、作品目录、上传素材、可编辑工作流及其历史分页和会话目录，内容报价必须点击“确认并创建任务”后才提交。本机Mock账号为 `alice@demo.flmh.invalid` / `DemoAlice2026!`、`bob@demo.flmh.invalid` / `DemoBob2026!`；管理员为 `admin@demo.flmh.invalid` / `DemoAdmin2026!`。用户JWT只在页面内存中，换用户会清空结果与运行编号。配置了 `AGENT_API_KEY` 时还需在访问设置填写独立的Agent访问凭据，使用 `X-Agent-Key` 发送；Java服务key不会进入浏览器。超时或连接中断后先用保留的运行编号读取状态，避免盲目新建重复请求。

Java账户和业务资源保存在H2文件中，上传素材元数据也由Java持久化，local实际文件位于 `LOCAL_STATE_DIR/assets`；手动启动复用同一 `LOCAL_STATE_DIR` 才保留原状态，验收脚本每次建立独立状态目录。Python默认 `CHECKPOINTER_BACKEND=memory`，检查点、会话目录和运行锁随进程退出消失。关闭本次手动服务时分别在原终端按 `Ctrl+C`，核实8001/8090监听已消失；不要停止任务开始前已有的服务。

或先登录并把JWT仅保存到当前PowerShell进程，再使用 CLI/示例：

```powershell
$login = Invoke-RestMethod -Method Post 'http://127.0.0.1:8080/api/v1/identity/login' `
  -ContentType 'application/json' -Body '{"email":"alice@demo.flmh.invalid","password":"DemoAlice2026!"}'
$env:FLMH_USER_TOKEN = $login.token
$headers = @{Authorization = "Bearer $($login.token)"}
# 若配置AGENT_API_KEY，再添加 $headers['X-Agent-Key'] = $env:AGENT_API_KEY
```

CLI在每次运行前核实用户JWT；identity-service重启后local令牌失效，需重新登录。新JWT带 `credentialStamp`，Java每次对照当前密码哈希核实；自行改密或管理员重置后旧JWT立即失效，Python身份入口同样返回401。升级前没有该标记的JWT不再接受，客户端须重新登录；迁移不自动改写旧用户密码，也不需要新增旧users表字段。



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

工作台商品目录通过 `GET /v1/products/catalog` 读取Java `/api/v1/products/catalog`，不经过LLM或Agent图。支持地区、来源、关键词/商品ID、规范类目、数值区间、新品、包邮、上下架、店铺和爆品筛选；`page` 为1–10000、`pageSize` 为1–100，返回 `products/total/page/pageSize/totalPages`。一次页面请求只转发一次有界目录查询，不逐商品调用接口。金额区间或 `sort=price` 必须明确 `currency`，没有币种时Python返回422，Java直接访问返回400；不换汇或跨币种排序。非法/重复查询字段被拒绝。

```powershell
Invoke-RestMethod 'http://127.0.0.1:8090/v1/products/catalog?region=US&source=LOCAL_FIXTURE&currency=USD&priceAmount=0%3A30&sort=price&direction=asc&page=1&pageSize=20' -Headers $headers
```

目录筛选当前快照，`metricScope=CURRENT_SNAPSHOT_WITH_MATCHING_DAILY`；日分项评分必须匹配当前快照、来源、币种、类目和输入指纹，缺失保持null，不在列表中重算。商品与指标来源分别显示，滚动7天销量不是当天销量；新品使用最早观测时间，不能推断源站上架日期。分页间商品变化时需重新查询，不提供跨请求固定快照。Python保留来源、币种和未知字段；真实数据规模与MySQL执行计划未验证。

查询计划还支持 `category_from_query`：从直接上游查询的首条商品读取类目，再作为下游类目过滤条件。必须同时在 `depends_on` 引用该上游，不得同时填写静态 `category`。上游无结果或类目缺失/非法时，下游查询失败，不放宽成无类目搜索。每条计划最多一次 MCP 查询，计划最多 4 条；同层独立查询并行，缓存同时考虑实际参数和上游结果。现有本地规则不会自动推导这种复杂计划；已提供明确计划示例：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m examples.cross_region_category
```

该示例在 Java 和 MCP 服务已启动时实际查询美国露营灯，再查询加拿大同类商品；使用 `SUPPLIED_EXAMPLE` 标记预先提供的计划，不调用或冒充模型规划。全自动验收脚本也会执行此例。

同一用户重复 `run_id` 和完全相同输入回放已有状态；不同用户的同名运行独立。服务端按用户ID与run_id派生检查点和锁键，用户不能通过工具参数自报owner。历史无归属检查点保留且不会被新用户读取。请求体发生变化返回 409。同一 run 的并发启动、确认、修订和刷新有运行协调锁；memory 模式在单进程内生效，Postgres 模式使用 PostgreSQL advisory lock。确认请求必须带当前报价 ID，避免修订后旧页面误确认新报价。任务幂等键包含报价 ID；缺少有效预算返回 `needs_input`，不猜金额、不请求报价。异步任务返回 `processing`，GET 运行记录刷新 Java 任务状态，直到已知终态。

确认生产前可以显式修订运行记录；`expected_revision` 防止并发覆盖，成功后 revision 加 1。商品、地区、预算有独立 `slotVersions`，结论保留 `conclusionDependencies`，查询按实际参数和上游结果指纹缓存；未受影响的查询复用，其余重新执行。修订将旧报价确认失效；已确认且尚未获得提交结果的运行和已提交文本任务禁止改写。媒体运行仅在服务端核实为 `FAILED` 后支持新报价修订，详见下方媒体主流程。

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
| Checkpoint / 会话目录 | `langgraph-checkpoint-postgres` 的 AsyncPostgresSaver、Postgres会话目录和advisory lock | 默认 InMemorySaver、内存目录与进程内锁；进程退出后状态消失 |
| 观测 | Langfuse v3 CallbackHandler；按配置启用并在退出时 flush/shutdown | 无 key 时禁用 |
| Text2SQL | Java Model Gateway 生成，SQLGlot AST 校验，asyncpg read-only transaction | 空权限或空连接默认关闭 |
| RAG | BGE-M3 HTTP 批量返回 dense/sparse；Elasticsearch PIT 内并行召回；RRF；BGE-Reranker 批量重排；原版本索引 `_mget` 父块回填；LangGraph 知识问答及内容参考资料节点 | 独立检索 HTTP 返回 503；Agent 知识请求返回 `needs_configuration` 与 TODO |

Postgres Checkpointer与会话目录的schema setup默认关闭；明确初始化数据库时才使用 `POSTGRES_CHECKPOINT_SETUP=true`，会话目录使用独立连接池和 `agent_session_directory` 表。旧的持久检查点只有经owner访问后才登记到目录，不自动认领无归属状态。BI 使用独立的 `BI_DATABASE_URI` 和只读数据库账号；不共用 Checkpointer 的写权限。BI HTTP和Agent BI都使用核实后的用户角色，`BI_SQL_ROLE_TABLES_JSON`只配置 `USER`/`ADMIN`/`SUPER_ADMIN` 的表权限。角色头、旧共享role token和 `AGENT_SQL_PRINCIPAL` 不能提升权限。

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
Set-Location 'D:\Happy\芳\flmh-p'
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\enterprise-20261117\v12'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = 'D:\rubbish\zhongjian\caches\python\flmh'
if (!(Test-Path -LiteralPath $env:TEMP)) { New-Item -ItemType Directory -Path $env:TEMP | Out-Null }
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m pip install -e .
# 接入 PostgreSQL / Langfuse 时分别安装 -e '.[postgres]' / -e '.[observability]'
```

运行测试：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\enterprise-20261117\v12'
$env:TMP = $env:TEMP
if (!(Test-Path -LiteralPath $env:TEMP)) { New-Item -ItemType Directory -Path $env:TEMP | Out-Null }
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m unittest discover -s tests -v
& 'D:\Code\nodejs\node.exe' --test tests/test_workbench_media.cjs tests/test_workflow_editor.cjs tests/test_sessions_ui.cjs tests/test_product_catalog.cjs tests/test_assets_ui.cjs
```

测试覆盖确认闸门、预算/币种、幂等、查询DAG、槽位失效、日统计、FAILED媒体明确字段修订、未知结果恢复、MCP确认、HTTP协议、RAG、Text2SQL、账号权限、商品目录、作品、通用工作流及会话/SSE。v12增加素材流式上传与下载、owner隔离、固定素材ID和工作流历史翻页。实际通过Python 265项、Node 88项及pip check；Java 401项/73个测试集和本机整链107/107通过。Chrome完成12项观察，服务与页面已收尾；限制和证据见[Java交付记录](../flmh-j/docs/delivery.md)。

**历史v11本机整合验证记录：Python 253项、Node最终62项和 `pip check` 通过；Java 360项/68个suite零失败、错误或跳过，六JAR离线构建通过；跨进程验收92/92通过。** 对应Java `d7c2692`、Python `5091363`。这些是历史v11结果，不代表v12已完成验收。真实PostgreSQL、Langfuse、BGE及ES未联调，没有性能基准或召回效果指标，完整生产级重构尚未完成。

历史v11的Chrome完成12项交互观察，包含12个排序选项、金额币种拦截、目录来源与null、详情、交给Agent不自动提交、媒体修订新报价10秒和空创作输入、确认后MOCKED零文件、改密自动退出清空目录/结果及重登恢复；当轮控制台无warning/error，page10关闭、原page3保留。当轮验收的10组进程、22个登记节点全部退出，收尾检查8001、8080–8085、8090无监听，原7890代理保留；此处不将历史状态当作本轮资源状态。

## 待配置与未验证项

登录后可展开“账号资料与用户管理”：自己资料与旧密码校验改密使用 `/v1/identity/profile`、`/v1/identity/password`；管理员通过 `/v1/identity/users` 及其子路径完成分页查询、创建、角色/状态/密码修改、积分与会员设置、删除。管理操作走受限HTTP入口，不暴露给LLM或MCP；Java再次核实当前目录角色，Python只转发服务端配置的内部凭据和当前用户JWT，响应按安全字段白名单输出。

账号数据由Java持久化到本地H2，重启不覆盖已有资料或密码；local JWT随身份进程重启失效。`credentialStamp`还使改密与管理员重置立即撤销旧JWT，包括重置为相同明文密码；改名/角色/状态仍读取当前目录。工作台积分请求结果未知后清空原调整并锁定目标，重新读取并选择用户后才允许下一次操作；**当前余额不能判断上一笔是否成功，积分API没有服务端幂等键**。页面离开/退出清空账号列表、资料和密码；请求不会自动重发。

`.env.example` 中所有服务地址/凭据空项由部署时填写。明天需决定 PostgreSQL Checkpointer 与只读 BI 库、Langfuse、BGE-M3/BGE-Reranker 服务、ES chunk 索引与认证方式。真实模型供应商与模型参数由 Java Model Gateway 维护。真实外部 adapter 联调、数据库权限/索引执行计划验证、跨主机负载和性能基准尚未运行。不得把本地 adapter 或 HTTP contract 测试当作生产数据、实际召回效果或完整生产部署验收。

Agent API与MCP使用用户JWT逐请求核实身份，检查点、会话目录与锁按用户隔离；可选X-Agent-Key仍只是额外服务访问边界。MCP仅绑定本机。商品目录筛选分页、商品历史和日统计工具、资料与管理员用户管理、作品和通用工作流已接入同源工作台；完整旧前端全部页面、密码找回、新任务计入旧视频用量聚合、支付等业务仍未完成。改密撤销已经实现，但不提供单设备登出或独立令牌撤销列表。旧项目只提供关联达人数量等商品指标，未据此虚构独立达人目录。

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

输入 `查美国旅行杯并制作图片和视频，预算10`，规则计划选择 `product_media`，经 LangGraph → MCP → Java `/api/v1/content/media-productions` 获取报价。默认一张图片再生成6秒、9:16视频；明确的时长、比例和提示词经统一 `media_options` schema校验，规则与模型计划走同一图，确认前不会提交供应商。商品限价与内容预算分别处理；明确使用“元/CNY”的预算不能用于USD报价，也不会自动换汇。未指定币种时按报价显示的币种确认。超过2000字符的媒体提示词（含商品和知识资料）返回可修订错误，不静默截断。

共享确认入口仍为 `POST /v1/agent/runs/{runId}/confirmation`，必须带当前报价 ID 与真正的布尔值。即使本地零元报价也要求确认。确认仅幂等创建 `QUEUED` 运行，后续操作均显式触发：

- `GET /v1/agent/runs/{runId}`：只读取 Java 已保存状态，可恢复上次状态读取中断。
- `POST /v1/agent/runs/{runId}/media/advance`：推进一次提交、存储或查询阶段，不携带正文。
- `POST /v1/agent/runs/{runId}/media/verify`：只核实 `SUBMISSION_UNKNOWN`，不重新提交付费请求。

工作台展示逐节点状态、来源、报价规格和产物记录，地址仅作文本。`MOCK_ONLY/MOCKED` 明确没有图片或视频文件。推进响应丢失后先读取状态；未知结果禁止直接推进，只有核实入口。固定两节点主流程的Java适配包括OpenAI Images/Responses、Gemini generateContent、旧项目TOAPIS xai/volcengine视频协议及私有OSS上传/下载；视频供应商成功先进入 `VIDEO_GENERATED`，转存成功才进入 `SUCCEEDED` 并归档作品。所有端点、模型、价格、凭据和结果origin白名单在Java `.env.example` 留空；供应商验证只运行本机合同测试。Kling/AGE仅接入下方通用DAG，不自动扩展此固定主流程。

`POST /v1/agent/runs/{runId}/revision`已接入失败媒体修订，工作台提供商品、地区、预算和媒体规格修改。图片/视频要求是空白的替换输入，留空不修改；比例和时长预填现值，完整合成提示词在新报价中核对。请求带整数 `expected_revision`；可通过 `slot_updates` 与 `media_updates` 提供明确字段，后者允许 `image_prompt`、`video_prompt`、`aspect_ratio`（1:1/3:4/4:3/9:16/16:9）和 `duration_seconds`（2–15整数）。自然语言按逗号、分号或换行分隔明确字段，例如“预算改成20美元；视频提示词：海边展示；时长改为10秒”。结构化字段优先；任意描述不自动当视频提示词，规则修订明确标记 `RULES`，模型计划也使用同一校验结构。

服务端只从当前owner会话取得原运行，再经Java读取核实，调用方不能指定原运行ID。必须为 `FAILED` 且没有未知提交节点；`SUBMISSION_UNKNOWN`、运行中和其他终态均拒绝。旧报价及确认失效，新报价展示 `mediaOriginRun`、`mediaRequest` 与 `reusedNodes`，确认后经MCP `media_production_revise`创建新媒体运行。仅预算/视频提示词/时长改变时保留图片输入；商品、地区、图片提示词或比例变化不会错误复用原图片。等待新确认时可以继续修订，仍引用同一原运行；提交响应丢失只能沿原幂等键恢复，不能改输入绕过未知结果。

以下仅在本人现有媒体运行已经确定FAILED时执行，不创建或伪造失败数据：

```powershell
$failed = Invoke-RestMethod "http://127.0.0.1:8090/v1/agent/runs/$mediaAgentRunId" -Headers $headers
$revision = @{expected_revision=$failed.revision; slot_updates=@{budget_max=20}; media_updates=@{video_prompt='海边展示'; duration_seconds=10}}
$newQuote = Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/agent/runs/$mediaAgentRunId/revision" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ($revision | ConvertTo-Json)
$newQuote.confirmationRequest
```

核对本次报价与复用节点后，使用前述confirmation接口并回传新 `quote_id` 明确确认。默认Mock正常结束为 `MOCKED`，不能用它演示FAILED分支；该分支的证据是本机针对性fixture测试，未修改数据库制造成功，也未运行真实供应商端到端修订。

## 我的作品与私有媒体

工作台“我的作品目录”通过同源 `/v1/works` 访问Java作品服务，可按模块/类型筛选、分页排序、读取正文和生产血缘、手动保存报告/脚本，以及勾选确认后软删除。列表每页最多50件且不包含正文大字段，血缘单独分页；`CONTENT_TASK`、`MEDIA_NODE`、`WORKFLOW_NODE`分别指向实际的内容任务、固定媒体节点和通用工作流节点。资源只对当前owner可见，管理员不默认获得他人的作品。

下面沿用登录时的 `$headers`，演示手动文本与同键重放；`reused=true`表示复用已有作品，来源为 `USER_SAVED`：

```powershell
$workHeaders = $headers.Clone()
$workHeaders['Idempotency-Key'] = 'work-' + [Guid]::NewGuid().ToString('N')
$workBody = @{module='manual-notes'; title='演示说明'; type='report'; content='用户保存的演示文字，不是模型输出。'} | ConvertTo-Json
$saved = Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/works' -Headers $workHeaders -ContentType 'application/json; charset=utf-8' -Body $workBody
Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/works' -Headers $workHeaders -ContentType 'application/json; charset=utf-8' -Body $workBody
Invoke-RestMethod 'http://127.0.0.1:8090/v1/works?type=report&page=1&pageSize=20&sort=createdAt_desc' -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8090/v1/works/$($saved.record.id)" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8090/v1/works/$($saved.record.id)/lineage?page=1&pageSize=20" -Headers $headers
```

同一 `POST /v1/works` 也接受本人已完成文本任务引用 `{taskId}`，或固定媒体成功节点引用 `{runId,nodeKey:"image"|"video"}`；不能混合手动文本字段，也不能提交用户ID、任意媒体URL或base64。正常成功任务自动归档，引用保存会复用已有作品。`DELETE /v1/works/{id}`保留幂等墓碑、任务和血缘，重放已删除作品返回409，不重新创建，也不删除共享OSS对象。

真实媒体归档后可在工作台“下载文件”：`GET /v1/works/{id}/file`先核对归属，再通过Java读取私有OSS稳定路径，按64KiB分块转发，最大200MiB。Chrome文件保存选择器直接写入选定文件，未提供该能力的浏览器会明确提示；不创建完整文件大小的内存Blob。Java转存视频只允许配置的精确结果origin，不跟重定向，下载/上传共用最多60秒期限；D盘 `MEDIA_TRANSFER_TEMP_DIR` 中的临时文件在响应结束后清理。Mock媒体没有作品或可下载文件，真实供应商/OSS只完成本机HTTP合同验证，未对外联调。

## 上传素材与固定素材ID

工作台“我的上传素材”经同源 `/v1/assets` 访问Java素材目录，提供原始文件上传、本人目录分页、信息查看和“用于当前节点”。文件来源始终为 `USER_UPLOAD`，不会计为模型生成作品：local把真实文件保存在Java的 `LOCAL_STATE_DIR/assets`，标记 `LOCAL_FILESYSTEM`；production使用 `PRIVATE_OSS`，其真实端点和凭据仍待配置。Python按块转发上传和鉴权下载，不把文件改成base64或整量缓存。

允许PNG/JPEG每张最多25MiB、4000万像素，MP4/WebM每段最多50MiB。Python入口要求单一 `Content-Type`、`Content-Length`、`Idempotency-Key`；Java继续核对实际字节、类型、长度和图片尺寸。视频上传仅验证容器签名，不保证编解码、时长或帧率；AGE执行视频输入必须另经可信ffprobe。临时上传文件由Java写入D盘 `MEDIA_ASSET_TEMP_DIR` 并在成功/失败后清理。local DAG即使引用真实上传，也只返回 `MOCKED`、`artifacts=[]`，不生成文件。

以下沿用登录时的 `$headers`。将 `$file` 改为已有PNG；`-InFile`发送原始字节并自动声明长度。响应未知或409处理中时保留相同文件、文件名和幂等键重试；同键不同输入会冲突，不能换键重投未知请求。

```powershell
$file = 'D:\path\reference.png'
$assetHeaders = $headers.Clone()
$assetHeaders['Idempotency-Key'] = 'asset-' + [Guid]::NewGuid().ToString('N')
$assetUrl = 'http://127.0.0.1:8090/v1/assets?fileName=' + [Uri]::EscapeDataString([IO.Path]::GetFileName($file))
$uploaded = Invoke-RestMethod -Method Post $assetUrl -Headers $assetHeaders -ContentType 'image/png' -InFile $file
Invoke-RestMethod -Method Post $assetUrl -Headers $assetHeaders -ContentType 'image/png' -InFile $file
Invoke-RestMethod 'http://127.0.0.1:8090/v1/assets?type=image&page=1&pageSize=20' -Headers $headers
$nodeAssets = @(@{type='image'; assetId=$uploaded.asset.id})
ConvertTo-Json -InputObject $nodeAssets -Compress
```

成功响应为 `{asset,reused}`，重放返回原素材及 `reused=true`。目录每页最多50项，工作台每页20项；`GET /v1/assets/{id}`读信息，`GET /v1/assets/{id}/file`鉴权下载，其他用户资源返回404；当前不提供删除或覆盖素材。浏览器在上传响应未知时保留原文件与幂等键，“重试同一上传”不会创建另一份。

节点固定素材JSON格式为 `[{"type":"image","assetId":"<返回的UUID>"}]`，或视频的 `type:"video"`；`assetId`与旧`url`二选一。“用于当前节点”只把引用填入当前节点编辑框，仍须手动点击应用节点、保存定义，再重新报价和确认。报价与运行按owner批量核实素材，输入指纹包含不可变素材ID及类型；SHA-256由素材元数据保存；真实执行只接受本人私有OSS素材或本运行上游已归档产物，固定外链仍拒绝，本地路径不会发送给供应商。独立图片编辑/遮罩流程尚未实现，工作流参考图生成协议见下一节。

## 通用可编辑工作流

工作台“可编辑工作流”提供定义目录、节点/连线/位置编辑、版本更新、报价与确认创建、逐次推进/核实/取消以及历史运行。此入口通过Python同源HTTP转发到Java；不经过LLM规划或MCP自动执行。定义限制20节点/400边，保存时校验环、引用及输入口；节点或商品/预算/来源运行变化会清除旧报价。定义创建没有服务端幂等键，保存响应未知时先重新读取目录核实，不盲目重复创建。

能力目录 `GET /v1/workflows/meta/capabilities`保留旧12种能力、历史启用标记和输入约束；旧AGE关闭时实际开放4种。全部支持零生成文件的Mock演示。Java通用DAG提供Kling文生/参考图视频，以及AGE分镜、文生、图生、首尾帧、混合参考和复刻协议adapter；支持本人上传的固定素材ID与本运行上游已归档私有OSS产物，固定外部URL真实执行仍拒绝。无参考图文生图和单图片xai/volcengine视频路径保留，参考输入按上游边顺序在先、固定素材在后组合，命名首尾帧口保持定义含义。

`gemini:image`是保留的能力ID，实际图片供应商须明确配置OpenAI或Gemini。现支持最多8张、合计25MiB的PNG/JPEG参考图，来源仅限已核实的owner/private OSS输入；OpenAI Images用multipart `/images/edits`，Responses用 `input_image`，Gemini generateContent用 `inlineData`。不静默截断、不接受任意外链，不由自然语言Agent自动选择执行。这里是工作流参考图生成，独立纯图片编辑流程/界面、遮罩和局部编辑仍未实现；本机HTTP协议测试不代表真实生成或质量验证。

这些Kling/AGE adapter只在本机HTTP fixture验证过协议，不代表真实生成或供应商联调完成；AGE视频输入要求 `MEDIA_AGE_FFPROBE_EXECUTABLE` 指向已有可信工具，本轮未安装或运行ffprobe。模型、地址、密钥、价格及存储配置继续留空，缺失时拒绝真实执行；未知提交无任务ID时不重投。模型和未适配参数不猜测或静默降级，完整能力表与空配置见[Java README](../flmh-j/README.md#通用可编辑工作流)。

默认本地模式的两节点示例先创建定义并展示报价：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8090/v1/workflows/meta/capabilities' -Headers $headers
$graph = @{name='本地图片到视频'; definition=@{nodes=@(
  @{key='image'; capability='gemini:image'; prompt='生成商品封面'; params=@{aspectRatio='9:16'}},
  @{key='video'; capability='xai:video'; prompt='让封面展示商品特点'; params=@{aspectRatio='9:16'; duration=6}}
); edges=@(@{parent='image'; child='video'; order=0})}}
$workflow = Invoke-RestMethod -Method Post 'http://127.0.0.1:8090/v1/workflows' -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ($graph | ConvertTo-Json -Depth 10)
$quote = Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/workflows/$($workflow.id)/quotes" -Headers $headers -ContentType 'application/json' -Body (@{definitionVersion=$workflow.version; productId='camping-lamp-001'; budgetMax=10} | ConvertTo-Json)
$quote
```

核对金额、币种、执行模式、商品与定义版本后，再单独执行明确确认。创建请求响应未知时保留相同 `$workflowHeaders` 与报价重放；不得通过换键重投绕过结果核实：

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/workflow-quotes/$($quote.quoteId)/confirmation" -Headers $headers -ContentType 'application/json' -Body '{"confirmed":true}'
$workflowHeaders = $headers.Clone()
$workflowHeaders['Idempotency-Key'] = 'workflow-' + [Guid]::NewGuid().ToString('N')
$workflowRun = Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/workflows/$($workflow.id)/runs" -Headers $workflowHeaders -ContentType 'application/json' -Body (@{quoteId=$quote.quoteId} | ConvertTo-Json)
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/workflow-runs/$($workflowRun.runId)/advance" -Headers $headers
Invoke-RestMethod -Method Post "http://127.0.0.1:8090/v1/workflow-runs/$($workflowRun.runId)/advance" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8090/v1/workflow-runs/$($workflowRun.runId)" -Headers $headers
Invoke-RestMethod "http://127.0.0.1:8090/v1/workflows/$($workflow.id)/runs?page=1&pageSize=20" -Headers $headers
```

本地终态为 `MOCKED`且 `artifacts=[]`，真实上传文件也不会因此变成生成作品。`PUT /v1/workflows/{id}`更新时携带`expectedVersion`及完整定义，`GET /v1/workflows/{id}/versions/{version}`读取旧版本，`DELETE`只归档定义并保留历史。运行快照、租约、心跳和fencing token由Java持久化，最多并行3个节点，每用户最多3个活跃运行、每定义最多1个活跃运行。

运行历史 `GET /v1/workflows/{id}/runs?page=1&pageSize=20` 返回 `runs/total/page/pageSize/totalPages`，pageSize为1–50。工作台每页20条，可点上一页、下一页和某次运行读取已有快照；只读取当前账号、当前定义的记录，不自动翻完整历史，也不触发生产。切换定义会重置页码，迟到响应不会覆盖新选择，空页与总数单独展示。

`POST /v1/workflow-runs/{id}/verify`只核实或转存已有结果，不提交新生成；`/cancel`阻止新提交，但不能撤销供应商已接受的任务，已有查询/转存仍可核实收尾。修订报价可传同定义已明确结束运行的`originRunId`，只复用成功且输入/依赖未变的节点；无供应商任务ID的未知提交保持阻塞。三种操作均不带正文。

## 会话目录与状态订阅

“我的会话”可以读取目录、选择会话、读取已有结果、改名、勾选确认删除，以及订阅/停止状态。目录使用游标分页，工作台每页10项，API `limit`为1–50；Postgres实现使用单条有界查询，不逐会话加载检查点。列表返回`{sessions,nextCursor}`，会话元数据含`runId/title/preview/status/version/createdAt/updatedAt/deleted`，尚未实现完整消息历史。

下面读取前面创建的 `local-request-001` 并改名。`expectedVersion`来自最新会话元数据，冲突返回409后应重新读取再决定，不自动覆盖：

```powershell
$sessionPage = Invoke-RestMethod 'http://127.0.0.1:8090/v1/agent/sessions?limit=20' -Headers $headers
$sessionPage
if ($sessionPage.nextCursor) {
  $cursor = [Uri]::EscapeDataString($sessionPage.nextCursor)
  Invoke-RestMethod "http://127.0.0.1:8090/v1/agent/sessions?limit=20&cursor=$cursor" -Headers $headers
}
$sessionRunId = 'local-request-001'
$session = Invoke-RestMethod "http://127.0.0.1:8090/v1/agent/sessions/$sessionRunId" -Headers $headers
$session.run
$renamed = Invoke-RestMethod -Method Patch "http://127.0.0.1:8090/v1/agent/sessions/$sessionRunId" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body (@{title='露营灯演示'; expectedVersion=$session.session.version} | ConvertTo-Json)
```

`GET /v1/agent/sessions/{runId}`返回`{session,run}`，只读取已有checkpoint；与可能刷新Java任务状态的 `GET /v1/agent/runs/{runId}`含义不同。`GET /v1/agent/runs/{runId}/events`是SSE状态流，同样不连接MCP刷新、不确认报价、不推进生产、不触发付费请求；每2秒重新核实身份并观察检查点，10秒无变化发心跳，正常观察60秒结束并发送`end`；发送阻塞最多额外5秒后断开。前端65秒自停，由用户手动重连，不后台无限轮询。

在当前PowerShell进程以请求头携带凭据订阅一次；不要把JWT或Agent key放入URL：

```powershell
$streamHeaders = @('-H', "Authorization: Bearer $($login.token)")
if ($env:AGENT_API_KEY) { $streamHeaders += @('-H', "X-Agent-Key: $env:AGENT_API_KEY") }
& curl.exe -N --max-time 70 @streamHeaders "http://127.0.0.1:8090/v1/agent/runs/$sessionRunId/events"
```

事件为`status`、`heartbeat`和`end`；`Last-Event-ID`可携带上次状态的哈希以抑制相同状态，不代表可回放全部历史事件。订阅响应未知或断开时先重新读取会话。改名/删除响应未知时，工作台锁定修改并要求重读；退出、换用户或切换会话会中止流并清空显示。

确实需要删除该演示会话时，再单独执行下列命令。软删除保留检查点和Java任务，不取消供应商请求；旧`runId`不能通过启动、确认、重试或修订复活。首次删除检查版本，重复删除返回同一墓碑；非owner和读取已删会话均返回404。

```powershell
$session = Invoke-RestMethod "http://127.0.0.1:8090/v1/agent/sessions/$sessionRunId" -Headers $headers
Invoke-RestMethod -Method Delete "http://127.0.0.1:8090/v1/agent/sessions/${sessionRunId}?expectedVersion=$($session.session.version)" -Headers $headers
```

## 来源日统计与评分

输入 `查询 camping-lamp-001 的日统计`，Agent 先读取商品来源，再按该来源调用 MCP `product_daily_statistics`；每次固定两次工具调用，日统计最多返回 30 条，MCP 可传 `limit`（1–100）和 `before_date`。响应 `productDaily` 保留评分版本、观测、趋势、限制说明与日期游标，不随条数增加请求或自动翻完全部历史。商品槽位更新会失效旧日统计。

日统计由 Java 内部重算用例生成，Agent 只读；没有已保存统计时明确返回空，不把快照差值冒充日报。`radar-v2-exact-date` 延续旧版分项评分，但 7/30 天增长要求精确日期基线；缺失基线保持 null。金额和类目聚合按来源、币种隔离，原始滚动 7 天销量不会改称当天销量。真实数据库查询计划及生产规模未验证。
