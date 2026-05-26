"""Phase 8.3 高级检索策略模块。

子模块：
    ircot —— Interleaving Retrieval with Chain-of-Thought（多轮检索 + 推理链）

设计原则：
    - 不借用 UltraRAG（PHASE_8_3_KICKOFF.md §二评估后决策直接自写）
    - 复用 RagRunner 的检索接口（self.search / _generate）
    - 不破坏现有 quick / agent 模式；通过 strategy 参数路由
"""
