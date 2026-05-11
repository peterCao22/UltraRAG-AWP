"""Repository 基础设施 —— ConnectionProvider + placeholder 抽象。

为什么需要 ConnectionProvider：
    SQLite 的 `?` 和 Postgres 的 `%s` 参数占位符不兼容；让 Repository 不直接
    依赖 sqlite3 / psycopg，而是通过 provider 拿连接 + 用 placeholder 拼 SQL。

Phase 5.1.5 只实现 SQLite provider，Phase 5.1.6 加 Postgres provider。
通过 get_default_provider() / set_default_provider() 全局切换。
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ConnectionProvider(Protocol):
    """统一连接获取接口。

    实现要点：
        - connect() 返回上下文管理器，with 进入时拿连接、退出时 commit/close
        - placeholder 区分 SQLite("?") 和 Postgres("%s")
        - row_factory 让 fetchall() 返回 dict-like 行（兼容现有 row_to_dict）
    """

    placeholder: str  # "?" for sqlite, "%s" for postgres
    backend_name: str  # "sqlite" / "postgres"

    @contextlib.contextmanager
    def connect(self) -> Iterator[Any]:  # type: ignore[override]
        """获取连接的上下文管理器。"""
        ...


class SqliteConnectionProvider:
    """SQLite 后端 —— 包装现有 custom_app.db.get_conn()。

    Phase 5.1.5 阶段的默认 provider；Phase 5.1.6 之后只在测试 / 显式切回时使用。
    """

    placeholder = "?"
    backend_name = "sqlite"

    @contextlib.contextmanager
    def connect(self) -> Iterator[Any]:
        from custom_app.db import get_conn  # 延迟 import 避免循环

        conn = get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------


_default_provider: Optional[ConnectionProvider] = None


def get_default_provider() -> ConnectionProvider:
    """获取全局默认 provider；首次调用按 .env 选择实现。"""
    global _default_provider
    if _default_provider is None:
        backend = os.environ.get("ULTRARAG_DB_BACKEND", "sqlite").strip().lower()
        if backend == "postgres":
            try:
                from custom_app.repositories.postgres_provider import (
                    PostgresConnectionProvider,
                )
            except ImportError as e:
                raise RuntimeError(
                    f"postgres backend selected but provider unavailable: {e}"
                ) from e
            _default_provider = PostgresConnectionProvider()
            logger.info("Repository backend: postgres")
        else:
            _default_provider = SqliteConnectionProvider()
            logger.info("Repository backend: sqlite")
    return _default_provider


def set_default_provider(provider: Optional[ConnectionProvider]) -> None:
    """显式设置 / 重置 provider（测试或运行时切换）。"""
    global _default_provider
    _default_provider = provider


# ---------------------------------------------------------------------------
# SQL placeholder helper
# ---------------------------------------------------------------------------


def adapt_sql(sql: str, provider: ConnectionProvider) -> str:
    """把 SQL 中的统一占位符 `?` 转成 provider 实际使用的占位符。

    Repository 用 `?` 写 SQL（SQLite 原生风格）；切 Postgres 时
    自动替换为 `%s`。注意：不处理 SQL 注释中的 `?`，但 Repository 里 SQL
    不应有内嵌注释（保持简单）。
    """
    if provider.placeholder == "?":
        return sql
    return sql.replace("?", provider.placeholder)


def fetch_all_as_dicts(cursor) -> list[dict[str, Any]]:
    """把 cursor.fetchall() 结果统一转成 list[dict]。

    SQLite Row 对象支持 dict() 转换；psycopg dict_row factory 已经是 dict。
    """
    rows = cursor.fetchall()
    if not rows:
        return []
    first = rows[0]
    # SQLite Row 支持 keys() + 字段索引
    if hasattr(first, "keys") and not isinstance(first, dict):
        return [{k: r[k] for k in r.keys()} for r in rows]
    # 已经是 dict（psycopg dict_row）或 list[tuple]（兜底）
    if isinstance(first, dict):
        return list(rows)
    # 不应到这里；让调用方明确处理
    return [dict(r) if isinstance(r, dict) else r for r in rows]


def fetch_one_as_dict(cursor) -> Optional[dict[str, Any]]:
    """fetchall 的单条版本。"""
    row = cursor.fetchone()
    if row is None:
        return None
    if hasattr(row, "keys") and not isinstance(row, dict):
        return {k: row[k] for k in row.keys()}
    if isinstance(row, dict):
        return row
    return None
