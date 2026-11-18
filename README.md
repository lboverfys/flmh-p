# FLMH 选品与内容 Agent · Python

LangGraph 负责计划、查询、修订与人为确认；通过 MCP 调用 Java 商品和内容服务。未填模型配置时，本机计划标记为 `RULES`，不会调用外部模型或产生费用。[联合演示与八个源码入口](https://github.com/lboverfys/flmh-j/blob/main/docs/interview.md) · [本轮交付](https://github.com/lboverfys/flmh-j/blob/main/docs/delivery.md)

## 标准启动入口：main.py

**Python 服务的唯一标准启动入口是根目录的 [main.py](main.py)。** 它在一个 Python 进程里启动 **Agent API**（健康检查 `http://127.0.0.1:8090/health/ready`）和 **MCP 工具服务**（`http://127.0.0.1:8001/mcp`）。不启动 Java 或前端，也不提供网页。工作台由 [flmh-j/frontend](https://github.com/lboverfys/flmh-j/tree/main/frontend) 独立提供，浏览器地址是 `http://127.0.0.1:8091/`。

在 **PyCharm** 打开 `D:\Happy\芳\flmh-p`，项目解释器选 `D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe`，右键根目录的 **`main.py` → 运行**，不需要填参数。用终端时，同一个入口只需一条命令，可在任意工作目录执行：

```powershell
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' 'D:\Happy\芳\flmh-p\main.py'
```

控制台会列出两个 Python 服务的地址。**停止两个 Python 服务：**在运行窗口输入 `stop` 并回车，或按 `Ctrl+C`。给同一命令追加 `--verify-only` 只检查两个 Python 服务能否完成启动并监听，随即退出。任一端口占用会报错退出，已存在的监听保持不动。

入口使用当前 Python 3.12+ 项目环境，默认读取本仓根目录的 `.env`（可选，模板见 [.env.example](.env.example)）。需要单独调试时，可分别运行 `flmh_agent.api` 或 `flmh_agent.adapters.inbound.mcp_tools` 模块。更改 MCP 端口时，记得将 `MCP_TOOLS_URL` 中的端口改为相同值。

**演示顺序：**先在 Java 仓运行 `./start.ps1`，打开工作台，使用 **Mock Alice → 登录**，可直接查商品和已发布榜单，不需要 Python。要使用对话，在另一个终端运行上面的 `main.py`，回到同一页面点击“刷新连接”。本机默认经 Java 网关（8080）核验用户令牌；保留的旧 `/v1/identity` 等兼容 API 不再是工作台的数据入口。Agent 示例：

- `查美国商品榜单前3名`：读取已发布的 Java 榜单，报告索引类型和来源；不把普通目录查询冒充榜单。
- `查 camping-lamp-001 的关联达人数`：读最多 30 条商品观测，缺值保留；不返回个人账号或名单。
- `请帮我在美国查露营灯并写口播脚本，预算不超过 30`：看方案、来源和本地报价，确认后生成。重复确认只返回同一任务。

## 阅读顺序

| 关注点 | 源码 | 可复现例子 |
| --- | --- | --- |
| 编排、确认与恢复 | [工作流](flmh_agent/application/workflow.py)、[服务](flmh_agent/application/service.py)、[检查点装配](flmh_agent/adapters/outbound/checkpoint.py) | [确认、预算与旧检查点](tests/test_workflow.py)；[失败修订](tests/test_media_revision.py) |
| 工具、依赖并行 | [计划模型](flmh_agent/domain/models.py)、[MCP 入口](flmh_agent/adapters/inbound/mcp_tools.py) | [跨地区绑定](tests/test_query_binding.py)、[榜单与达人观测](tests/test_market_tools.py) |
| 槽位版本 | [商品、地区、预算及依赖](flmh_agent/domain/slots.py) | 修改一项条件，比较响应的 `slotVersions`、`invalidated`、`reusedQueries` |
| 知识、评测与 BI | [混合检索](flmh_agent/application/rag.py)、[BGE/ES 适配](flmh_agent/adapters/outbound/bge_http.py)、[评测器](flmh_agent/evaluate.py)、[Text2SQL](flmh_agent/application/text2sql.py) | 无模型时使用下面的标注和人工排序检验计分逻辑；BI 未配置时不执行 SQL |

## 本机检查

在 Python 仓目录，复用现有项目环境，无需安装或下载：

```powershell
$py = 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe'
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\resume-core-20261118'; $env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
& $py -B -m unittest discover -s tests
& $py -B -m flmh_agent.evaluate --judgments examples/rag-evaluation/judgments.json --run examples/rag-evaluation/scripted-ranking.json
```

评测器按问题等权平均 `Recall@k`（默认 5）；MRR 取提供的完整排名中第一个相关结果的名次倒数。明确返回空列表计零；漏题、重复命中或题集版本不符会报错。标注里的文档 ID 对应旧项目商品雷达和工作流模块；三问及排序是新写的 `SCRIPTED_FIXTURE`。有实际检索输出后，为同一题集分别导出稠密基线与混合结果，标明 `RECORDED_RETRIEVAL`，重复传 `--run` 对比。交付中没有真实召回效果、Token 节省或线上耗时数据。

生产选择在 [.env.example](.env.example) 保持空值：LLM 经 Java 模型网关；Embedding 和重排端点/模型、ES、PostgreSQL 检查点、BI 只读库、Langfuse 均按实际配置启用。当前演示用内存检查点，关闭 Agent 就不会保留会话。旧版完整接口及平台外围功能见 [详细参考](REFERENCE.md)。
