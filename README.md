# FLMH Python Agent Demo

本地 Agent 使用 LangGraph 编排确定性 MOCK_RULES 计划，通过 MCP Streamable HTTP 工具访问 Java Demo Gateway。没有配置 LLM 时不会调用模型供应商。

## 启动

先按 [Java Demo 说明](../flmh-j/README.md) 在本机启动 Gateway（默认 `http://127.0.0.1:8080`）。然后开两个 PowerShell 窗口：

窗口一启动 MCP 工具服务：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$env:JAVA_GATEWAY_URL = 'http://127.0.0.1:8080'
$env:MCP_HTTP_HOST = '127.0.0.1'
$env:MCP_HTTP_PORT = '8001'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.mcp_server
```

窗口二启动 Agent：

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m flmh_agent.demo --request '请帮我在美国查露营灯并写口播脚本，预算不超过 30'
```

Agent 展示商品和本地 Mock 报价后会等待输入。输入 `y` 才确认报价并创建内容任务；其他输入都会停止在创建任务之前。`--confirm` 可用于自动化演示，表示调用者已明确确认。

重复运行相同请求会复用稳定幂等键。Java 返回的任务状态和生成草稿均为 Demo/Mock 数据。

## 配置与验证

环境变量模板见 `.env.example`。`LLM_PROVIDER`、`LLM_MODEL`、`LLM_API_KEY`、`EMBEDDING_PROVIDER`、`EMBEDDING_MODEL`、`VECTOR_STORE_URL` 保持空值；明天再确定供应商、模型、凭据来源和向量库。

安装依赖时使用 D 盘环境和缓存：

```powershell
$env:TEMP = 'D:\rubbish\zhongjian\temp\flmh\demo'
$env:TMP = $env:TEMP
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m pip install --cache-dir 'D:\rubbish\zhongjian\caches\python\flmh' -r requirements.txt
```

运行测试：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe' -m unittest discover -s tests -v
```

`flmh_agent.rag` 只定义稠密/稀疏召回、RRF、重排和父块回填边界；没有向量库或 Embedding 配置，不代表真实检索结果。
