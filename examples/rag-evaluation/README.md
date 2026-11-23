# 运营规则 RAG 对照基准

`operations-v2/` 是上一版待审语料：6 份固定版本的原文，140 个父块、191 个子块，108 道候选题。每题附原文短引、行号和父子块 ID；复核人留空，所有题均为 `pending`。默认命令仍兼容 64 题的 `operations-v1/`。本目录根部的三问 `SCRIPTED_FIXTURE` 仅演示计分，各版本候选题用于后续审核。

`operations-v3/` 是面向跨境 TikTok 全链路运营扩展的待审语料：10 份本地专题运营手册（`examples/rag-evaluation/operations-v3/corpus/*.md`），基于 `child_size=200`、`child_stride=130` 原生切分为 **310 个父块、662 个子块**，并配套生成 **300 道 `pending` 待复核候选题**（每份手册 30 题，顶层 `reviewer` 留空，全部附带经 `rag_corpus` 校验的原文行号、唯一引文及父子块 ID）。与 v1/v2 一样，300 道 `pending` 候选题用于人工复核与后续独立实验，不作为未复核的基准成绩或实测排名。

v3 候选题采用卖家问法。按章节标题去编号逐题检查，标题命中为 0。两道跨段题各绑定两个必需父块。正式冻结默认要求至少 300 道获批题及逐题裁决，当前获批数为 0。

可用 `rag_benchmark prepare` 验证或导出 `operations-v3` 复核包：

```powershell
& $py -B -m flmh_agent.rag_benchmark prepare --source-root D:\Happy\芳\flmh-p --manifest examples/rag-evaluation/operations-v3/sources.json --candidates examples/rag-evaluation/operations-v3/candidates.json --out-dir "$work/review-v3"
# 人工逐题完成复核后执行
& $py -B -m flmh_agent.rag_benchmark freeze --source-root D:\Happy\芳\flmh-p --manifest examples/rag-evaluation/operations-v3/sources.json --review "$work/review-v3/review.json" --min-approved 300 --out "$work/judgments-v3.json"
```

## 语料与授权

| 来源 | 选段用途 |
| --- | --- |
| `flmh/docs/product-radar-operation-manual.md` | FLMH 操作手册，涵盖 FastMoss 集采、印尼等市场代号、筛选口径、选品元小秘和运营 SOP |
| `flmh/docs/product-radar-data-foundation.md` | TKi8 基座方案，涵盖类目映射、受限预览、GMV 指标、监控和权限。按方案内容标注，实施状态由业务方核实 |
| `flmh/docs/PRODUCT.md`（v2） | 产品矩阵、会员、积分；区分已发布的 Kling/Veo 与路线图中的 Seedance |
| `flmh/docs/frontend-refactor-plan.md`（v2） | 工作台操作、提示、文案、主题与无障碍 |
| `flmh/docs/superpowers/specs/2026-04-19-tki8-ai-design.md`（v2） | SKU、积分预算、撤回、SSE 资产事件与上下文规则；属于设计规格 |
| `flmh/docs/superpowers/plans/2026-04-21-tki8-ai-stage1-kickoff.md`（v2） | 品线级工具、交付物与灰度验收；只取业务章节，不索引脚手架 |

`operations-v1/sources.json` 固定旧仓提交 `af8a6d97c1bb46f87e56e2f7fbac95ecd72faffb`、两份 UTF-8 原文的 SHA-256 和所选行段。运行时读取本机已有的旧仓，统一换行为 LF 后校验整份文件。只对清单内的选段建索引，使用现有切块器的 3000 字父块、800 字子块和 700 字步长，各段独立切分。已将这 **20 个父块、28 个子块** 的实际正文和行段登记在 `operations-v1/chunks.jsonl`，可直接按候选表中的 ID 阅读。`prepare` 从原仓重建相同记录。文档指纹、选段或切分版本变化会改变语料版本和块 ID。

v2 固定同一原仓提交，重新划分行段。v1 的 64 题重定位到新父子块，新增 44 题。候选深度 20 时，单路最多覆盖 20/191（约 10.5%）的子块；这只是规模比例，不是 Recall。资料所有者决定外发权限：编码端会收到选段和问题，重排端会收到问题和候选子块。索引范围以所选清单为准，相关性标注留在本地。

## 离线准备与人工复核

从 Python 仓根目录使用项目现有 Python 3.12+ 环境。以下 PowerShell 示例将产物与同盘临时目录放在当次独立目录，`--out-dir` 或输出文件名须尚未使用，父目录须已存在。

```powershell
Set-Location 'D:\Happy\芳\flmh-p'
$py = 'D:\rubbish\zhongjian\envs\flmh\agent\Scripts\python.exe'
$src = 'D:\Happy\芳\flmh'
$v2 = 'examples/rag-evaluation/operations-v2'
$manifest = "$v2/sources.json"
$runId = [guid]::NewGuid().ToString('N')
$work = "D:\rubbish\zhongjian\artifacts\flmh-rag\$runId"
[System.IO.Directory]::CreateDirectory($work) | Out-Null
& $py -B -m flmh_agent.rag_benchmark prepare --source-root $src --manifest $manifest --candidates "$v2/candidates.json" --out-dir "$work/review"
```

`prepare` 的默认输入为 `operations-v1/sources.json` 和 `operations-v1/candidates.json`（相对示例目录）。在其他工作目录调用时，用 `--manifest` 与 `--candidates` 指定相应文件。输出的 `chunks.jsonl` 可按 ID 查到原文、所属父块、原文件行段与版本。`review.json` 是可编辑的候选表。

**扩充自己的语料时**，先把已授权的原文和行段写入一份新的来源清单，再用下面的命令生成新块 ID。命令打印数据集名称、语料指纹、父块数和子块数，供标注与核对规模。现有 v1 只有 28 个子块，在 `k=5`、乘数为 4 时，单路候选上限为 20/28（约 71%），适合检验流程。正式实验应同时注明子块数和候选深度。

```powershell
& $py -B -m flmh_agent.rag_benchmark prepare --source-root $src --manifest "$work/sources.json" --chunks-only --out-dir "$work/new-chunks"
```

用新指纹和块 ID 创建候选表，逐题附上原文行号、引文，并将状态填为 `pending`。准备复核表时显式传入新的 `--manifest` 和 `--candidates`。后续 `freeze`、`index`、`record` 也使用同一份 `--manifest`。

1. 打开 `review.json`，按每题 `evidence` 的文档、行号和引文回看原文及 `chunks.jsonl`。核实问题是否可答、答案是否有歧义，关注品牌名、市场缩写和本地业务用语。
2. 检查其他父块是否也足以回答，逐条增删证据。每条填写 `document`、`line`、`quote`、`parent`、`child`。引文须在指定行内唯一出现且落在所填子块。跨段题可保留多个相关父块。
3. 在查看任一系统的排序前，逐题将 `status` 设为 `approved` 或 `rejected`，并在顶层 `reviewer` 填写复核人代号。复核表保存每一道题的裁决。清单建议正式对照纳入 50–100 道以上问题，试跑可先冻结较小的已复核题集。批准时确认的是语义相关性，脚本核对的是原文位置与块归属。
4. 冻结复核结果。保存这份复核表和输出标签，在同一对照实验中使用同一版本。

```powershell
& $py -B -m flmh_agent.rag_benchmark freeze --source-root $src --manifest $manifest --review "$work/review/review.json" --out "$work/judgments.json"
```

## 获得实验授权后的索引与记录

`index` 和 `record` 使用当前进程环境中的检索配置。设置 `EMBEDDING_BACKEND=bge_m3_http`、`BGE_M3_URL`、`BGE_M3_MODEL`、`BGE_M3_DIMENSION`、`BGE_RERANKER_URL`、`BGE_RERANKER_MODEL`、`ELASTICSEARCH_URL`。两个 BGE 地址均填完整端点，向量维数填该部署的实际值。清单指定的模型为 `BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3`。按服务需要提供对应的 `*_API_KEY`。ES 需支持 dense kNN、sparse_vector、PIT 和 msearch。使用现有 BGE HTTP 契约与请求超时，可通过 `REQUEST_TIMEOUT_SECONDS` 指定超时。运行者应同时留存模型部署版本、ES 版本和代码版本。

```powershell
$prefix = "rag-eval-ops-$runId"
& $py -B -m flmh_agent.rag_benchmark index --source-root $src --manifest $manifest --index-prefix $prefix
& $py -B -m flmh_agent.rag_benchmark record --source-root $src --manifest $manifest --index-prefix $prefix --judgments "$work/judgments.json" --out-dir "$work/rankings" --k 5 --candidate-multiplier 4 --batch-size 8
```

- `index` 要求一个全新的 `rag-eval-…` 前缀，覆盖进程内的 `ELASTICSEARCH_INDEX` 选择。先检查前缀占用，建索引遇到同名冲突即停止。每个源文档经现有入库器按最多 32 块批量嵌入和写入，核对数量后封存物理索引并发布该文档的别名。命令成功时把发布回执写到标准输出。中途失败会保留该前缀的诊断现场，重新准备索引使用新前缀。清理由索引所有者按实际归属处理。
- `record` 核对文档清单、嵌入模型、维数、向量字段和写保护，然后对清单指定的物理索引打开**一个 PIT（固定读取快照）**。全部题目和三种方法共用这个快照。每批 1–8 题，每题只嵌入一次。每批一次 ES `_msearch`，每题用同一模型分别重排稠密候选和混合候选，再对三路父块的并集执行至多一次 `_mget`。分片失败、任一路重排条数不齐或父块与原文不符都会中止导出。
- **dense-only** 单独取 kNN Top-k 子块。**dense-rerank** 取深度为 `k × candidate-multiplier` 的稠密候选并重排。**hybrid** 复用这份稠密候选，加上同深度的稀疏候选，按线上逻辑做 RRF 融合，再用同款 BGE 模型重排。乘数 1–10，候选深度上限 100。三路都按子块名次取首次出现的父块，最多 k 个。多个子块属于同一父块时保留实际长度。
- 成套的 `dense.json`、`dense-rerank.json` 和 `hybrid.json` 含 `RECORDED_RETRIEVAL`、相同的冻结标签指纹、语料版本、物理索引、模型、排名深度与 PIT 摘要。`trace.jsonl` 按题记录子块顺序，`hybrid_dense` 与 `dense_reranked` 分别是同一批稠密子块重排前后的次序，`hybrid_sparse`、`rrf` 和 `reranked` 可追踪稀疏候选从召回到最终排序的变化。写包使用同盘临时目录，成功关闭 PIT 后才发布到目标目录。

## 计分与查看具体题目

```powershell
& $py -B -m flmh_agent.evaluate --judgments "$work/judgments.json" --run "$work/rankings/dense.json" --run "$work/rankings/dense-rerank.json" --run "$work/rankings/hybrid.json" --k 5 --compare-cases --output "$work/report.json"
```

Recall@k 为前 k 名满足的必需证据组数除以总组数，再逐题等权平均。`equivalent_groups` 中任一父块命中即满足该组，其他父块各自都是必需证据。`all_required_at_k` 给出覆盖全部必需组的题目比例。`mrr` 按完整记录计算首个相关父块的倒数名次，`mrr_depth` 标明实际记录深度，`mrr_at_k` 与 `mrr_k` 给出截断结果。

`scores` 列出 dense-only、dense-rerank、hybrid 各组得分。`recovered` 与 `regressed` 比较浅层基线和混合检索在前 k 名中能否覆盖所有必需证据组。`sparse_recovered` 比较使用同一重排器的两路。逐题命中可回溯至 `trace.jsonl`。

`sparse_regressed` 记录 dense-rerank 满足全部证据组而 hybrid 未满足的题目。`still_missed` 记录所有参评方法都未覆盖全部必需组的题目。旧两路记录可省略 `dense-rerank.json`。
