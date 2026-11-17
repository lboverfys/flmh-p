# FLMH 选品与内容 Agent · Python

LangGraph 负责计划、查询、修订与人为确认；通过 MCP 调用 Java 商品和内容服务。未填模型配置时，本机计划标记为 `RULES`，不会调用外部模型或产生费用。[联合演示与八个源码入口](../flmh-j/docs/interview.md) · [本轮交付](../flmh-j/docs/delivery.md)

```powershell
& 'D:\Happy\芳\flmh-j\start.ps1'
```

在工作台 `http://127.0.0.1:8090/` 使用 **Mock Alice → 登录**。三个请求可直接输入：

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
