"""
日志统一初始化入口（仅在 Flask 进程启动时调用一次）。

为什么单独成文件：
  - app.py 启动时多种导入路径会触发 `getLogger(__name__)`，
    若没有提前挂 handler，所有 INFO/DEBUG 都会被静默吞掉。
  - 每个模块的 `logger.info(...)` 必须经过这里挂的 handler 才能落到
    控制台或 logs/ 目录。
  - 显式接管 root logger 后，第三方库（faiss/qdrant_client/google
    httpx 等）也会按这里的级别走，便于统一管控噪音。

输出目标（默认）：
  - 控制台 StreamHandler  ：INFO 及以上
  - logs/app.log          ：DEBUG 及以上（应用全量日志）
  - logs/kg_ingest.log    ：仅 KG 抽取与 ingest 阶段（便于 KG 排障）

可通过环境变量调整：
  - ULTRARAG_LOG_LEVEL_CONSOLE  : 控制台级别（默认 INFO）
  - ULTRARAG_LOG_LEVEL_FILE     : 文件级别（默认 DEBUG）
  - ULTRARAG_LOG_DIR            : 日志目录（默认项目根 logs/）
  - ULTRARAG_LOG_DISABLE_FILE=1 : 关闭文件 handler（CI / 容器场景）
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# 复用同一份 setup 结果，避免 Werkzeug reloader / pytest 重复挂 handler。
_SETUP_DONE = False

_DEFAULT_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# KG / ingest 相关的 logger 名前缀；后续要单独写到 kg_ingest.log
_KG_LOGGER_PREFIXES = (
    "custom_app.services.kg_extractor",
    "custom_app.services.kgstore",
    "custom_app.services.kg_search",
    "custom_app.api.kb",  # ingest 主入口
)


def _resolve_level(env_name: str, default: int) -> int:
    """读取环境变量里的日志级别，支持 'DEBUG'/'INFO' 字符串或数字。"""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    return getattr(logging, raw.upper(), default)


def _resolve_log_dir() -> Path:
    """日志目录：优先环境变量，否则项目根（custom_app/.. = 项目根）下的 logs/。"""
    custom = os.environ.get("ULTRARAG_LOG_DIR", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "logs"


class _KgIngestFilter(logging.Filter):
    """只放行 KG 抽取与 ingest 阶段的日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return any(record.name.startswith(p) for p in _KG_LOGGER_PREFIXES)


def setup_logging(*, force: bool = False) -> Optional[Path]:
    """初始化全局日志。

    参数：
        force: 已初始化过时是否强制重挂（用于测试）。

    返回：
        日志目录 Path（未启用文件输出时返回 None）。
    """
    global _SETUP_DONE
    if _SETUP_DONE and not force:
        return _resolve_log_dir() if not _file_disabled() else None

    console_level = _resolve_level("ULTRARAG_LOG_LEVEL_CONSOLE", logging.INFO)
    file_level = _resolve_level("ULTRARAG_LOG_LEVEL_FILE", logging.DEBUG)

    formatter = logging.Formatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT)

    root = logging.getLogger()
    # 清掉已有 handler，避免 Flask debug reload / pytest 多次注入产生重复行。
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(min(console_level, file_level))

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    log_dir: Optional[Path] = None
    if not _file_disabled():
        log_dir = _resolve_log_dir()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 目录建不出来不致命，退回控制台模式即可
            console_handler.setLevel(min(console_level, logging.WARNING))
            root.warning("无法创建日志目录 %s: %s，已降级为仅控制台输出", log_dir, exc)
            log_dir = None
        else:
            app_handler = RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=10 * 1024 * 1024,  # 单文件 10MB
                backupCount=5,
                encoding="utf-8",
            )
            app_handler.setLevel(file_level)
            app_handler.setFormatter(formatter)
            root.addHandler(app_handler)

            kg_handler = RotatingFileHandler(
                log_dir / "kg_ingest.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            kg_handler.setLevel(file_level)
            kg_handler.setFormatter(formatter)
            kg_handler.addFilter(_KgIngestFilter())
            root.addHandler(kg_handler)

    # 把第三方常见噪音库压到 WARNING 起步，避免淹没业务日志
    for noisy in ("urllib3", "httpx", "httpcore", "qdrant_client", "neo4j"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _SETUP_DONE = True
    logging.getLogger(__name__).info(
        "logging 初始化完成: console=%s file=%s dir=%s",
        logging.getLevelName(console_level),
        logging.getLevelName(file_level),
        str(log_dir) if log_dir else "<disabled>",
    )
    return log_dir


def _file_disabled() -> bool:
    return str(os.environ.get("ULTRARAG_LOG_DISABLE_FILE", "")).strip().lower() in (
        "1", "true", "yes",
    )
