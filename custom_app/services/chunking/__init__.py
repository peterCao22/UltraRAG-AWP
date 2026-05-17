"""Phase 8.2 切分增强模块。

子模块：
    contextual —— 让 Gemini 给每个 chunk 生成「文档级上下文摘要」，
                  追加到 chunks.jsonl 的 `context` 字段，
                  embedding 时拼到 contents 前，提升 RAG 召回。

设计原则：
    - 与现有 docx_parser / parsers 解耦：只在 ingest pipeline 中作为独立 stage 插入
    - 失败降级：单 chunk context 生成失败时 context="" 仍可索引（不阻塞 ingest）
    - 幂等：chunks.jsonl 已含 context 字段时跳过重生（重建索引不重新调 Gemini）
"""
