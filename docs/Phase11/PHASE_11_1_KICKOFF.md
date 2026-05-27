# Phase 11.1 性能优化启动文档

> 启动时间：2026-05-27
> 上游：[PHASE_8_SUMMARY.md §五](../Phase8/PHASE_8_SUMMARY.md#五phase-8-之外发现的性能问题待优化) 已批准的 3 项
> 用户已在局域网部署：
> - Embedding: `http://192.168.8.44:8021/v1/embeddings` （Qwen3-Embedding-8B，4096 维）
> - Reranker: `http://192.168.8.44:8022/rerank` （Qwen3-Reranker-8B）

---

## 一、现状性能基线（来自 PHASE_8_SUMMARY.md §5.2）

每次问答耗时分布：

| 阶段 | 首次 | 缓存后 | 是否每问发生 |
|---|---|---|---|
| Query embedding（Gemini API） | 1266ms | **1271ms** | ✅ 每问 |
| Qdrant 向量检索 | 68ms | 63ms | ✅ 每问 |
| Reranker（bge-reranker-v2-m3 GPU） | **8244ms** | 57ms | 仅首次慢 |
| chunks.jsonl 查 self._rows | <1ms | <1ms | ✅ 每问 |
| LLM 生成（Gemini / Claude） | **30-120 秒** | 30-120 秒 | ✅ **主瓶颈** |
| 图片转 data URL | 100-500ms / 张 | 100-500ms / 张 | 视答案而定 |

**用户报告**：每个问题约 2 分钟响应（首次更慢）。

---

## 二、3 项优化（按 ROI 排序）

### 优化 A：Reranker 启动预热 — 0.5 天

**目标**：消除首次问答的 10 秒模型加载延迟。

**改造点**：[`rag_runner.py init()`](../../custom_app/services/rag_runner.py) 末尾跑一次空 reranker 调用触发加载。

**风险**：极低（最坏退回原行为）。

### 优化 B：Quick mode 改流式输出 — 1 天

**目标**：首 token 体感大幅提升（从 ~12s 等待到 ~1-2s 看到首字）。

**现状**（[`rag_runner.py:1952-1957`](../../custom_app/services/rag_runner.py#L1952)）：

```python
if normalized_mode == "quick":
    # 注释："vLLM/OpenAI-compatible streaming can hang on some local gateways..."
    answer_raw = self._generate(prep["prompt_text"]).strip()  # 一次性拿全答案
```

**改造点**：
1. quick 模式启用流式：调 `_generate_stream` 替代 `_generate`
2. 前端 `onChunk` 已支持累积渲染（[main.js:1229](../../custom_app/frontend/main.js#L1229)）
3. 兼容性：Gemini / Claude / vLLM 三种 backend 各自流式协议略有差异；保留非流式降级路径

**风险**：中（之前注释提到"local gateway 可能 hang"，需要在 vLLM 上重新验证）。

### 优化 C：本地 Embedding 模型替代 Gemini API — 1-2 天

**目标**：消除 1.3 秒/问的跨国网络往返。

**用户已部署**：`http://192.168.8.44:8021/v1/embeddings` Qwen3-Embedding-8B（4096 维）。

**改造点**：

| 文件 | 改动 | 工时 |
|---|---|---|
| 新建 `custom_app/services/local_embedder.py` | 封装 OpenAI 兼容 `/v1/embeddings` 调用 | 0.5 天 |
| `parameter.yaml` | 加 `embed_backend: gemini \| local` 配置项 | 5 分钟 |
| `google_embedder.py` | `embed_texts / embed_query` 按 backend 路由 | 0.5 天 |
| `vectorstore/qdrant_store.py` | `DEFAULT_EMBED_DIM` 改可配置；按 backend 取 768/4096 | 15 分钟 |
| **重建 agv_demo / ifs_docs 索引** | embed_backend 切换后必须重建 Qdrant collection（旧 768 vs 新 4096 维不兼容） | 1 小时 |
| 评测对比 | 跑 agv_demo / ifs_docs baseline 对比新旧 embedding | 2 小时 |

**新旧 embedding 对比**：

| 维度 | Gemini API（旧） | Qwen3-Embedding-8B（新） |
|---|---|---|
| 模型 | gemini-embedding-001 | Qwen3-Embedding-8B |
| 维度 | 768（截断） | 4096（原生） |
| 单次延迟 | 1.3 秒（含网络） | 50-200ms（局域网 + GPU） |
| 月成本 | 视配额 | 一次性硬件 + 电费 |
| 多语言 | ✅ 多语种 | ✅ Qwen3 中英 + 多语种 |
| 离线 | ❌ 跨国 | ✅ 局域网 |
| Qdrant collection 大小 | 56 × 768 = 43KB | 56 × 4096 = 230KB（5.3×） |

**风险**：
- 维度变化 → 必须重建 Qdrant collection（agv_demo 56 / ifs_docs 16 个 chunk，重建 30s）
- 检索质量可能略不同 → 评测对比验证（容忍 ±2pp）

### 优化 D（额外建议）：本地 Reranker 替代 bge-reranker-v2-m3 — 0.5 天

**目标**：消除 reranker 首次 10 秒加载 + GPU 显存占用，让本地 RAG 服务彻底独立于 Python GPU 推理。

**用户已部署**：`http://192.168.8.44:8022/rerank` Qwen3-Reranker-8B。

**改造点**：

| 文件 | 改动 | 工时 |
|---|---|---|
| 新建 `custom_app/utils/remote_reranker.py` | 调用 `/rerank` HTTP 接口 | 0.5 天 |
| `parameter.yaml rag_rerank.*` | 加 `backend: local \| remote` + `remote.url` | 5 分钟 |
| `rag_runner.py` | 按 backend 选 Local / Remote Reranker | 15 分钟 |
| 评测对比 | 跑 agv baseline 验证 Qwen3-Reranker vs bge-reranker | 1 小时 |

**额外收益**：
- Flask 启动不再加载 reranker 模型（节省 ~10s 首次开销 + ~2GB GPU 显存）
- 局域网调用 ~50-100ms 比 GPU 本地 ~57ms 仅多 ~50ms，可接受

**风险**：bge-reranker-v2-m3 vs Qwen3-Reranker-8B 排序结果可能不同 → 评测验证。

---

## 三、推荐执行顺序

按风险递增 + 收益递减排序：

| 步 | 任务 | 工时 | 阻塞 |
|---|---|---|---|
| 1 | **优化 A** Reranker 启动预热 | 0.5 天 | 无 |
| 2 | **优化 B** Quick mode 流式输出 | 1 天 | 无 |
| 3 | **优化 C** 本地 Qwen3-Embedding | 1-2 天 | 需重建 Qdrant + 评测对比 |
| 4 | **优化 D** 本地 Qwen3-Reranker | 0.5 天 | 评测验证 |

**总工时 3-4 天**。

### 评测验证套件（C + D 都需要）

```bash
# 跑当前生产配置 baseline（参照点）
python -m custom_app.scripts.eval_custom_app --kb agv_demo --save-baseline

# 切换到新 embed / rerank 后
python -m custom_app.scripts.eval_custom_app --kb agv_demo --output data/eval/phase11_1/agv_demo__qwen3.json

# 对比：若 Recall@5 在 ±2pp 容忍带内 → 性能优化获胜，上线；否则回退
```

**容忍门槛**：Recall@5 / MRR / Hit@1 任一指标退化 >2pp 则该项优化暂不上线。

---

## 四、配置规划

### 4.1 parameter.yaml 改动

```yaml
# Phase 11.1 新增
embed_backend: gemini   # gemini | local
embed_backend_configs:
  gemini:
    # 沿用现有 google_embedder.py 行为
    api_key_env: GOOGLE_API_KEY
    model: gemini-embedding-001
    dim: 768
  local:
    base_url: http://192.168.8.44:8021/v1
    model: qwen3-embedding-8b
    dim: 4096
    timeout_sec: 30

# rag_rerank 段扩展
rag_rerank:
  enabled: true
  backend: local        # local | remote
  local:
    model_name_or_path: C:\reranker\bge-reranker-v2-m3
    device: auto
    batch_size: 4
  remote:
    base_url: http://192.168.8.44:8022
    timeout_sec: 30
    # Qwen3-Reranker-8B 无需指定 batch_size（服务端自管）
```

### 4.2 env 全局开关（应急用）

```
ULTRARAG_EMBED_BACKEND=gemini|local
ULTRARAG_RERANK_BACKEND=local|remote
```

env > yaml，让运营可以快速回滚到 Gemini / bge-reranker 而不重新部署。

---

## 五、风险缓解

| 风险 | 缓解 |
|---|---|
| 局域网服务 192.168.8.44 不可达 → 整站 RAG 挂 | 启动时 ping 检查；失败自动降级到 Gemini API + bge-reranker（保留双栈代码） |
| Qwen3-Embedding 检索质量比 Gemini 差 | 评测对比验证，<-2pp 退化则不上线 |
| Qdrant collection 维度迁移失误 | 重建前备份 chunks.jsonl + embedding.npy（已被 .gitignore 排除，可手动 cp） |
| 流式输出在某些 backend 不稳定 | 保留 quick 非流式作 fallback；env `ULTRARAG_STREAM_DISABLE=1` 可关 |
| Reranker 服务延迟突增 | 加 timeout=30s；超时跳过 reranker（已有降级逻辑 `_rerank_skip_reason`） |

---

## 六、立即可启动

按"先无依赖再有依赖"顺序：

1. **优化 A**（Reranker 预热）—— 30 分钟内完成
2. **优化 B**（quick 流式）—— 半天
3. **优化 C**（Qwen3 embedding）—— 重建索引 + 评测
4. **优化 D**（Qwen3 reranker）—— 评测对比
