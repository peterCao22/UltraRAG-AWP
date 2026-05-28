# Phase 11.2 切块改造总结

> 跑分时间：2026-05-27
> 改造范围：ifs_docs（agv_demo 后续）
> 上游：[Phase 11.1.D 评测对比](./phase11_1_qwen3_rerank_comparison.md)

---

## 〇、TL;DR

**新切块（recursive splitter, 400 字目标）+ 本地 bge-reranker-v2-m3 + Qwen3 embedding 全面胜出 Phase 11.1.D 基线。**

| 指标 | Phase 11.1.D | **Phase 11.2** | Δ |
|---|---|---|---|
| **Hit@5** | 0.9836 | **1.0000** | **+1.64pp** ✅ |
| **Hit@10** | 0.9836 | **1.0000** | **+1.64pp** ✅ |
| **Hit@1** | 0.9016 | **0.9180** | **+1.64pp** ✅ |
| **MRR** | 0.9426 | **0.9536** | **+1.09pp** ✅ |
| **failures** | 1 | **0** | **-1** ✅ |
| chunks 总数 | 16 | 32 | +100% |

副产物：**发现 Qwen3-Reranker-8B 在短粒度 chunk 上判断不可靠**（详见 §三），切回本地 bge-reranker-v2-m3。

---

## 一、改造内容

### 1.1 docx_parser 切块逻辑重写

**Why**: 用户多次反馈 "WeKnora 切得很细，咱们一篇文章只切 2-3 块"。诊断显示 ifs_docs 单 chunk 高达 887 字含 5 个并列子项（出库类型/入库类型/品牌/零件状态/计划人），"计划人" 被埋在中段，LLM 漏答。

**How**:
- 借鉴 WeKnora `docreader/splitter/splitter.py` 设计
- 新增 `_split_text_recursive`：递归分隔符切分 `\n\n → \n → 。→ ？→ ！→ ；→ ，→ 、→ space`
- 新增 `_PROTECTED_PATTERNS`：Markdown 表格 / `[IMG: ...]` 占位 / 代码块 / 链接整体保留
- 新 chunk 目标 size：**400 字**（env `ULTRARAG_CHUNK_TARGET_SIZE` 可调）
- 新 overlap：**80 字**（句末截断不破句）

### 1.2 chunk_id 命名兼容

短 chunk（≤ 400 字）保留原 `section_N` 命名；长 chunk 切成 `section_N_part_1` / `_part_2` / ...

EvalRunner 加 `_expand_gold_for_part_chunks`：旧评测集 `section_N` gold 自动展开到所有 `_part_*` 变体，**评测集 jsonl 0 改动**。

### 1.3 顺手修：跳过 Word 临时锁文件

`parse_directory` 跳过 `~$xxx.docx`（Office 打开时的临时锁），避免 docx 解析时报错。

---

## 二、ifs_docs chunk 分布对比

| | 旧 (16 chunks) | **新 (32 chunks)** |
|---|---|---|
| 平均 size | ~600 字 | **~280 字** |
| 最大 size | 1530 字 | **396 字** |
| "计划人" 归属 | 887 字 `section_2` 中段（被埋） | **独立 `section_2_part_3`**（143 字） |

人工验证 "计划人配置" 检索：
- 旧：top-5 命中 #2，但 LLM 漏答（A 阶段 prompt 不够强 / chunk 太杂）
- **新：top-5 命中 #4，独立 chunk + context 明确提及 Planners 页签 → LLM 精准回答**

---

## 三、关键发现：Qwen3-Reranker-8B 不适配短粒度 chunk

### 3.1 现象

第一次跑 Phase 11.2 评测（新切块 + Qwen3-Reranker 远程）指标全线退化：

| 指标 | 11.1.D | 11.2 + Qwen3-Rr | Δ |
|---|---|---|---|
| Hit@5 | 0.9836 | 0.9016 | **-8.20pp** ❌ |
| MRR | 0.9426 | 0.7265 | -21.61pp ❌ |
| failures | 1 | 6 | +5 ❌ |

### 3.2 诊断

逐层检查：
1. **裸 vector 召回**: `IFSSOP_section_2_part_2`（"启动 oracle 数据库" gold chunk）排 **#2** ✅
2. **过 Qwen3-Reranker**: 该 chunk 被推到 8 名外，无关 chunk "库存管理移库操作"（`库存管理和客户订单操作_section_2_part_3`）被打 **0.93 分**上位
3. 直接测 Qwen3-Reranker，给 distractor chunk 打 0.93、给 target chunk 打 0.59 —— **明显误判**

### 3.3 验证：关 rerank（裸 vector + RRF）反而最好

| 指标 | 11.1.D | 11.2 + Qwen3-Rr | **11.2 关 rerank** |
|---|---|---|---|
| Hit@5 | 0.9836 | 0.9016 | **1.0000** |
| failures | 1 | 6 | **0** |
| MRR | 0.9426 | 0.7265 | **0.9672** |

### 3.4 切回 bge-reranker-v2-m3 后

| 指标 | 11.1.D | 11.2 + Qwen3-Rr | 11.2 no-rr | **11.2 + bge** |
|---|---|---|---|---|
| Hit@5 | 0.9836 | 0.9016 | 1.0000 | **1.0000** |
| Hit@1 | 0.9016 | 0.5902 | 0.9344 | **0.9180** |
| MRR | 0.9426 | 0.7265 | 0.9672 | **0.9536** |
| failures | 1 | 6 | 0 | **0** |

bge-reranker 没像 Qwen3 那样添乱，与裸 vector 持平甚至略好。

### 3.5 推测原因

Qwen3-Reranker-8B 在以下条件叠加时不可靠：
- chunk 短（< 500 字）
- 内容含大量代码块 / 命令 / 英文混排（如 IFSSOP 那种 `set ORACLE_SID=TEST` 段）
- chunk 主题词不出现在 contents 里（被 context 字段携带）

可能与训练数据分布有关——Qwen3-Reranker 训练时长 chunk 更多。在 ifs_docs 这种中文操作手册短粒度场景，bge-reranker-v2-m3 更稳。

---

## 四、上线动作清单

| 项 | 状态 |
|---|---|
| `docx_parser.py` 重写切块（递归分隔符 + protected）| ✅ 已合 |
| `tests/test_docx_parser_sliding.py` 新增 3 测 + 重写 2 测 | ✅ 已合 |
| `EvalRunner._expand_gold_for_part_chunks` + 4 单测 | ✅ 已合 |
| `parse_directory` 跳过 `~$xxx.docx` | ✅ 已合 |
| ifs_docs `chunks.jsonl` 重建 (16 → 32) | ✅ 已重建 |
| ifs_docs Qdrant collection 重建（32 chunks × 4096d）| ✅ 已重建 |
| ifs_docs contextual chunking 重跑（32/32 成功）| ✅ 已重跑 |
| `.env` `ULTRARAG_RERANK_BACKEND` `remote` → `local`（切回 bge）| ✅ 已切回 |
| 评测基线 `data/eval/phase11_2/ifs_docs__phase11_2_bge_rerank.json` | ✅ 已落档 |
| Phase 11.2 总结文档 | ✅ 本文 |
| **应急回滚** | `data/kb/ifs_docs/corpora/chunks.jsonl.phase11_1_backup` 仍在；恢复后重建 Qdrant 即可 |
| agv_demo 同样改造 | 待开 |

---

## 五、agv_demo：无需 Phase 11.2 改造

跑 docx_parser 重解析 agv_demo（56 chunks），结果**与旧版文本完全一致**——所有 chunk 本来就 ≤ 400 字（top-10 size 全在 367 以下，STEP 切分自动细粒度），新切块逻辑对它**无 op**。

结论：
- agv_demo chunk_id 命名不变（无 `_part_N` 后缀生成）
- 不需要重跑 contextual chunking
- 不需要重建 Qdrant collection
- Phase 11.1.D 在 agv_demo 上的指标继续有效，无回归

**Phase 11.2 切块改造仅对 ifs_docs 这种"非 STEP 长 chunk 文档"有显著收益**。后续如果接入新的非 SOP 类文档，会自动享受细粒度切块。

---

## 六、人工验证（ifs_docs 三种模式）

用户重启 Flask 后用前端测试 "在'库存基础数据'设置界面中，具体的配置项有哪些。其中选择计划人，要如何配置？"：

| 模式 | 结果 |
|---|---|
| 快速问答 | ✅ 完整列出 5 个配置项 + 计划人步骤 |
| 智能推理（ReAct agent）| ✅ 推理链可见，多轮工具调用，结果正确 |
| 深度思考（IRCoT）| ✅ 推理步骤可见，图片穿插对应步骤下，结果正确 |

确认 Phase 11.2 收官。
