"""Phase 8.1 离线评测体系。

子模块：
    schema   —— EvalItem / EvalResult dataclass + 校验
    dataset  —— jsonl IO
    metrics  —— 字符串生成指标（剥离自 UltraRAG evaluation.py）
    runner   —— 评测驱动（驱动 RagRunner 跑检索/生成 + 汇总指标）

设计原则：
    - **0 行 UltraRAG 依赖**：metrics 已剥离；runner 直接 import custom_app.services.rag_runner
    - **指标分两层**：检索指标（Recall@k / MRR / nDCG / Hit@1）+ 生成指标（accuracy / f1 / rouge-l / em / cover-em）
    - **生成层默认关**：CI 只跑检索；本地 `--with-generation` 才调 LLM（省 Gemini 配额）
"""
