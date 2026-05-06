from __future__ import annotations
from typing import Any, Dict, Optional, Sequence, Type

from .base import BaseIndexBackend

_INDEX_BACKENDS: Dict[str, Optional[Type[BaseIndexBackend]]] = {
    "faiss": None,
    "milvus": None,
}
_INDEX_IMPORT_ERRORS: Dict[str, Exception] = {}

try:
    from .faiss_backend import FaissIndexBackend

    _INDEX_BACKENDS["faiss"] = FaissIndexBackend
except Exception as exc:
    _INDEX_IMPORT_ERRORS["faiss"] = exc

try:
    from .milvus_backend import MilvusIndexBackend

    _INDEX_BACKENDS["milvus"] = MilvusIndexBackend
except Exception as exc:
    _INDEX_IMPORT_ERRORS["milvus"] = exc


def create_index_backend(
    name: str,
    contents: Sequence[str],
    logger,
    config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> BaseIndexBackend:
    backend_key = name.lower()
    if backend_key not in _INDEX_BACKENDS:
        raise ValueError(
            f"Unsupported index backend '{name}'. "
            f"Available options: {', '.join(sorted(_INDEX_BACKENDS))}."
        )
    backend_cls = _INDEX_BACKENDS.get(backend_key)
    if backend_cls is None:
        exc = _INDEX_IMPORT_ERRORS.get(backend_key)
        raise ImportError(
            f"Backend '{backend_key}' requires optional dependency not installed.\n"
            f"Original error: {exc}"
        )
    return backend_cls(contents=contents, config=config or {}, logger=logger, **kwargs)


__all__ = [
    "BaseIndexBackend",
    "create_index_backend",
]