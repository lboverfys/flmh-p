# 运营规则 RAG 对照基准

`flmh-radar-ops-v1` 提供 64 道待人工复核的规则问答。每题附原文短引、行号、父块 ID 和覆盖该处的子块 ID。同目录的 `judgments.json` 和 `scripted-ranking.json` 提供原有的三问 `SCRIPTED_FIXTURE` 计分示例。本题集的实验状态为待运行，真实指标与案例由未来的配对记录产生。

## 语料与授权

| 来源 | 选段用途 |
| --- | --- |
| `flmh/docs/product-radar-operation-manual.md` | FLMH 操作手册，涵盖 FastMoss 集采、印尼等市场代号、筛选口径、选品元小秘和运营 SOP |
| `flmh/docs/product-radar-data-foundation.md` | TKi8 基座方案，涵盖类目映射、受限预览、GMV 指标、监控和权限。按方案内容标注，实施状态由业务方核实 |

`operations-v1/sources.json` 固定旧仓提交 `af8a6d97c1bb46f87e56e2f7fbac95ecd72faffb`、两份 UTF-8 原文的 SHA-256 和所选行段。运行时读取本机已有的旧仓，统一换行为 LF 后校验整份文件。只对清单内的选段建索引，使用现有切块器的 3000 字父块、800 字子块和 700 字步长，各段独立切分。已将这 **20 个父块、28 个子块** 的实际正文和行段登记在 `operations-v1/chunks.jsonl`，可直接按候选表中的 ID 阅读。`prepare` 从原仓重建相同记录。文档指纹、选段或切分版本变化会改变语料版本和块 ID。

资料所有者决定这些选段及衍生问题可用于哪些实验服务、可向谁展示。编码端会收到选段和问题，重排端会收到问题和候选子块。公开或外发范围需要对应授权。索引范围限于两份规则文档，相关性标注留在本地计分。

## 离线准备与人工复核

从 Python 仓根目录使用项目现有 Python 3.12+ 环境。以下 PowerShell 示例将产物与同盘临时目录放在当次独立目录，`--out-dir` 或输出文件名须尚未使用，父目录须已存在。

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$py = 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe'
$src = 'D:\Happy\芳\flmh'
$runId = [guid]::NewGuid().ToString('N')
$work = "D:\rubbish\zhongjian\artifacts\flmh-rag\$runId"
[System.IO.Directory]::CreateDirectory($work) | Out-Null
& $py -B -m flmh_agent.rag_benchmark prepare --source-root $src --out-dir "$work/review"
```

`prepare` 的默认输入为 `operations-v1/sources.json` 和 `operations-v1/candidates.json`（相对示例目录）。在其他工作目录调用时，用 `--manifest` 与 `--candidates` 指定相应文件。输出的 `chunks.jsonl` 可按 ID 查到原文、所属父块、原文件行段与版本。`review.json` 是可编辑的候选表。

1. 打开 `review.json`，按每题 `evidence` 的文档、行号和引文回看原文及 `chunks.jsonl`。核实问题是否可答、答案是否有歧义，关注品牌名、市场缩写和本地业务用语。
2. 检查其他父块是否也足以回答，逐条增删证据。每条填写 `document`、`line`、`quote`、`parent`、`child`。引文须在指定行内唯一出现且落在所填子块。跨段题可保留多个相关父块。
3. 在查看任一系统的排序前，逐题将 `status` 设为 `approved` 或 `rejected`，并在顶层 `reviewer` 填写复核人代号。复核表保存每一道题的裁决。清单建议正式对照纳入 50–100 道以上问题，试跑可先冻结较小的已复核题集。批准时确认的是语义相关性，脚本核对的是原文位置与块归属。
4. 冻结复核结果。保存这份复核表和输出标签，在同一对照实验中使用同一版本。

```powershell
& $py -B -m flmh_agent.rag_benchmark freeze --source-root $src --review "$work/review/review.json" --out "$work/judgments.json"
```

## 获得实验授权后的索引与记录

`index` 和 `record` 使用当前进程环境中的检索配置。设置 `EMBEDDING_BACKEND=bge_m3_http`、`BGE_M3_URL`、`BGE_M3_MODEL`、`BGE_M3_DIMENSION`、`BGE_RERANKER_URL`、`BGE_RERANKER_MODEL`、`ELASTICSEARCH_URL`。两个 BGE 地址均填完整端点，向量维数填该部署的实际值。清单指定的模型为 `BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3`。按服务需要提供对应的 `*_API_KEY`。ES 需支持 dense kNN、sparse_vector、PIT 和 msearch。使用现有 BGE HTTP 契约与请求超时，可通过 `REQUEST_TIMEOUT_SECONDS` 指定超时。运行者应同时留存模型部署版本、ES 版本和代码版本。

```powershell
$prefix = "rag-eval-ops-$runId"
& $py -B -m flmh_agent.rag_benchmark index --source-root $src --index-prefix $prefix
& $py -B -m flmh_agent.rag_benchmark record --source-root $src --index-prefix $prefix --judgments "$work/judgments.json" --out-dir "$work/rankings" --k 5 --candidate-multiplier 4 --batch-size 8
```

- `index` 要求一个全新的 `rag-eval-…` 前缀，覆盖进程内的 `ELASTICSEARCH_INDEX` 选择。先检查前缀占用，建索引遇到同名冲突即停止。每个源文档经现有入库器按最多 32 块批量嵌入和写入，核对数量后封存物理索引并发布该文档的别名。命令成功时把发布回执写到标准输出。中途失败会保留该前缀的诊断现场，重新准备索引使用新前缀。清理由索引所有者按实际归属处理。
- `record` 核对文档清单、嵌入模型、维数、向量字段和写保护，然后对这两份文档的具体物理索引打开**一个 PIT（固定读取快照）**。全部题目、全部批次和两种方法共用该快照。每批 1–8 题，每题只嵌入一次。每批一次 ES `_msearch`，用现有单问题接口按题重排，再对两路父块的并集执行至多一次 `_mget`。分片失败、重排条数不齐或父块与原文不符都会中止导出。
- **dense-only** 对子块单独请求 kNN Top-k。**hybrid** 的稠密和稀疏两路各取 `k × candidate-multiplier` 个候选，走与线上检索共用的 RRF 与 BGE 重排函数。乘数 1–10，候选深度上限 100。两路均按子块名次取首次出现的父块，最多 k 个。多个子块属于同一父块时结果可短于 k，保留真实长度。
- 成套的 `dense.json` 和 `hybrid.json` 含 `RECORDED_RETRIEVAL`、相同的冻结标签指纹、语料版本、物理索引、模型、排名深度与 PIT 摘要。`trace.jsonl` 按题记录实际子块顺序，含基线、混合候选的稠密路与稀疏路、RRF 和重排。写包使用同盘私有临时目录，成功关闭 PIT 后才发布到目标目录。

## 计分与查看具体题目

```powershell
& $py -B -m flmh_agent.evaluate --judgments "$work/judgments.json" --run "$work/rankings/dense.json" --run "$work/rankings/hybrid.json" --k 5 --compare-cases --output "$work/report.json"
```

Recall@k 对每题计算“前 k 名中的相关父块数 / 该题标注的相关父块数”，再对题目等权平均。MRR 取完整已记录排名中首个相关父块名次的倒数，再按题平均。空列表两项均计零。`--k` 可以调低到记录深度以内，MRR 仍按完整记录计算。

在 `report.json` 中，`recovered` 表示前 k 名由零命中变为命中，`still_missed` 表示两路均为零命中，`regressed` 表示仅基线命中。每项附问题、相关父块及两路父块顺序。查看实际存在的一道找回题和一道双路失败题，用题号关联 `trace.jsonl` 与复核表，核对相关子块在哪一步出现或消失。数组为空时照实保留。混合稠密路的候选比基线更深，归因时需同时检查两路候选和重排。实测数字与案例的适用范围是本次冻结题集、语料和部署快照。
