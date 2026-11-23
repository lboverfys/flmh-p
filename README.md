# TKi8（FLMH）出海增长 Agent · Python

基于 LangGraph 的跨境电商选品分析、运营知识检索、Text2SQL 问数、多语种文案与图生视频工作流编排服务，通过 FastMCP（`8001`）对接 Java 业务微服务（`8080–8085`）。未填外部模型配置时，规划使用显式规则 `RULES`，本地语料检索由 `RAG_LOCAL_CORPUS_FALLBACK` 显式开启。

[联合演示与八个亮点源码](https://github.com/lboverfys/flmh-j/blob/main/docs/interview.md) · [交付基线与边界](https://github.com/lboverfys/flmh-j/blob/main/docs/delivery.md) · [Java 工程入口](https://github.com/lboverfys/flmh-j/blob/main/README.md)

## 核心能力与近期对齐重构

- **五槽位版本化上下文与多语种控制（`flmh_agent/domain/slots.py`）**：独立管理 `product`、`region`、`product_max_price`、`budget_max` 与 `target_language` 五个版本槽位；仅修改语种或预算时复用既有选品事实，精准失效旧文案与报价。
- **跨工具分层并行 DAG（`planning.py` / `mcp_tools.py`）**：四类商品工具（目录、榜单、趋势、达人）按依赖分层并行。RAG 是独立的前置检索节点，只读 BI 走互斥路径。 `category_from_query` 与 `product_id_from_query` 绑定直接上游产物，对外输出 `executionBatches`。
- **成稿转图生视频交接（`/v1/agent/runs/{runId}/handoff-media`）**：使用同一任务的成功回执及非空终稿。新媒体运行保留来源运行、任务与全文 SHA-256，默认提示取正文前 220 字，每条最多 600 字。
- **Text2SQL 只读执行与安全校验（`text2sql.py` / `mysql_sql.py`）**：支持 `SQL_DIALECT` 切换 MySQL / PostgreSQL，默认对齐新 Java `product-service` 表结构；通过 `AsyncMysqlReadOnlyRunner` 下发只读事务、超时上限与行封顶，并开放 `/v1/bi/validate` SQL AST 安全校验端点。
- **RAG 混合检索与 310 节 / 300 题跨境语料（`rag.py` / `operations-v3`）**：支持 BGE-M3 + ES 稠密/稀疏 + RRF + BGE-Reranker + 父块回填流水线，显式启用 `RAG_LOCAL_CORPUS_FALLBACK=true` 时可自动加载本地语料检索；新增 10 份 TikTok 跨境运营手册 [operations-v3](examples/rag-evaluation/README.md)（310 父块、662 子块、300 道 `pending` 候选题）。

MySQL 使用项目环境安装 `.[mysql]`。采集的商品原始快照版本为 `source-v1`，其他指标快照保留写入方版本。日表使用 `SQL_DAILY_SCORE_VERSION=radar-v2-exact-date`，旧指标表使用 `SQL_DEFAULT_SCORE_VERSION=v2`。本机独立 MySQL 9.4.0 的只读账号实际执行前端示例与两种分表查询。

预算修订复用商品与知识证据。检索证据同时绑定知识查询、语料版本、商品、市场和语种。知识库可指定 `RAG_CORPUS_VERSION`，缺省时已发布 ES 文档索引的集合或本地清单提供版本。`RAG_LOCAL_CORPUS_FALLBACK` 默认关闭。

## 工作台管理员配置

管理员可在 B2B SaaS 工作台的 **更多 → 系统配置中心** 中选择 **Python Agent / MCP** 维护规划器、模型网关与 RAG/BGE 连接、MySQL BI 数据源、访问凭据、回退开关与请求超时。全部配置项通过共享目录 `FLMH_SETTINGS_DIR` 与主密钥读取，动态配置于后续请求生效，监听端口与存储后端于重启时生效。

## 标准启动入口：main.py

**Python 服务的唯一标准启动入口是根目录的 [main.py](main.py)。** 它在单个进程内同时启动 **Agent API**（`http://127.0.0.1:8090/health/ready`）与 **MCP 工具服务**（`http://127.0.0.1:8001/mcp`）。独立 B2B SaaS 前端工作台位于 [flmh-j/frontend](https://github.com/lboverfys/flmh-j/tree/main/frontend)（`http://127.0.0.1:8091/`）。

在 PyCharm 打开 `D:\Happy\芳\flmh-p`（解释器选 `D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe`）直接运行 **`main.py`**，或在终端执行：

```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' 'D:\Happy\芳\flmh-p\main.py'
```

输入 `stop` 或按 `Ctrl+C` 退出，附加 `--verify-only` 可仅做端口绑定预检。环境变量和空白生产占位模板见 [.env.example](.env.example)。

### 本机可直接演示的代表性输入

- `查美国商品榜单前3名`：读取 Java 已发布榜单并保留来源与评分版本，不拿普通目录冒充榜单。
- `分析美国露营灯的近30天趋势和关联达人数据`：触发 Parallel DAG 并行调用趋势与达人 MCP 工具。
- `在印尼帮我挑选防晒精华并生成 id-ID 印尼语带货口播脚本，预算 20`：演示 `target_language` 槽位解析、报价中断与确认创建任务。
- 前端智能选品页还内置独立 **运营知识库 (RAG)** 与 **BI Text2SQL 问数与校验** 实验台，以及 **一键转图生视频** 表单。

## 源码导航

| 关注点 | 核心源码 | 验证用例 |
| --- | --- | --- |
| 编排、交接与检查点 | [workflow.py](flmh_agent/application/workflow.py)、[service.py](flmh_agent/application/service.py)、[checkpoint.py](flmh_agent/adapters/outbound/checkpoint.py) | [test_workflow.py](tests/test_workflow.py)、[test_media_revision.py](tests/test_media_revision.py) |
| 多语种与并行 DAG | [models.py](flmh_agent/domain/models.py)、[planning.py](flmh_agent/domain/planning.py)、[mcp_tools.py](flmh_agent/adapters/inbound/mcp_tools.py) | [test_query_binding.py](tests/test_query_binding.py)、[test_stage4_alignment.py](tests/test_stage4_alignment.py) |
| 5 槽位版本与失效矩阵 | [slots.py](flmh_agent/domain/slots.py) | 检查响应 `slotVersions`、`invalidated`、`reusedQueries` |
| RAG / 语料 v3 / Text2SQL | [rag.py](flmh_agent/application/rag.py)、[rag_corpus.py](flmh_agent/rag_corpus.py)、[text2sql.py](flmh_agent/application/text2sql.py)、[mysql_sql.py](flmh_agent/adapters/outbound/mysql_sql.py) | [test_rag.py](tests/test_rag.py)、[test_rag_v3_corpus.py](tests/test_rag_v3_corpus.py)、[test_rag_benchmark.py](tests/test_rag_benchmark.py) |

## 本机自动化测试

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$py = 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe'
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\verify-local'; $env:TMP = $env:TEMP
& $py -B -m unittest discover -s tests
& $py -B -m flmh_agent.evaluate --judgments examples/rag-evaluation/judgments.json --run examples/rag-evaluation/scripted-ranking.json
```

最近一次全量离线单元回归结果为 **346 / 346 通过**（0 失败 / 0 错误 / 0 跳过）。[RAG 评测说明](examples/rag-evaluation/README.md) 与 [Agent 对照基准](examples/agent-evaluation/README.md) 提供了手工审核及接入真实模型/ES 后的实测方法；阶段二真实集群 benchmark 均留待接入环境后手动运行。
