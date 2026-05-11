"""Phase 5.1.5 — Repository 层。

把所有 SQL 调用集中到 Repository，便于 Phase 5.1.6/5.1.7 切换 SQLite → Postgres。

设计原则：
    - Repository 暴露业务方法，不暴露 SQL（如 create_kb / list_active / mark_failed）
    - 所有 Repository 共享一个 ConnectionProvider（sqlite_provider / postgres_provider）
    - 传入/返回 dict，与现有 row_to_dict 风格一致
    - 不引入 ORM，SQL 写明白；切 Postgres 时只改 placeholder 风格（? → %s）

模块结构：
    base                  —— ConnectionProvider Protocol + SQLite 默认实现
    kb_repository         —— knowledge_bases 表
    job_repository        —— kb_jobs 表
    document_repository   —— kb_documents 表
    session_repository    —— kb_sessions + kb_session_messages
    role_repository       —— roles + role_kb_permissions
    agent_config_repository —— kb_agent_configs
    kg_repository         —— kg_entities + kg_relations
"""

from custom_app.repositories.agent_config_repository import AgentConfigRepository
from custom_app.repositories.base import (
    ConnectionProvider,
    SqliteConnectionProvider,
    get_default_provider,
    set_default_provider,
)
from custom_app.repositories.document_repository import DocumentRepository
from custom_app.repositories.job_repository import JobRepository
from custom_app.repositories.kb_repository import KbRepository
from custom_app.repositories.kg_repository import KgRepository
from custom_app.repositories.role_repository import RoleRepository
from custom_app.repositories.session_repository import SessionRepository

__all__ = [
    # base
    "ConnectionProvider",
    "SqliteConnectionProvider",
    "get_default_provider",
    "set_default_provider",
    # repositories
    "AgentConfigRepository",
    "DocumentRepository",
    "JobRepository",
    "KbRepository",
    "KgRepository",
    "RoleRepository",
    "SessionRepository",
]
