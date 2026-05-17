"""Phase 8.2.1 Contextual Chunking —— 给每个 chunk 生成「文档级上下文摘要」。

灵感来自 Anthropic 2024 Contextual Retrieval：把 chunk 在整个文档中的位置 / 作用
压缩成 50-100 字摘要，embedding 时拼到 contents 前，帮助检索定位脱离原文档的 chunk。

设计原则（与 [[project-phase8-12-roadmap]] 共识）：
    - **Context 范围**：整篇文档（SOP 多为 5-20k tokens，一次输入 Gemini 即可）
    - **回填策略**：本期 ifs_docs / agv_demo 全量回填；其他 KB 走 ingest 时自然生成
    - **失败降级**：单 chunk context 生成失败 → context="" 仍可索引，不阻塞 ingest
    - **幂等**：chunks.jsonl 已有 `context` 字段且非空时跳过该 chunk（重建索引不重生成）

调用示例：
    enricher = ContextEnricher()
    n_generated, n_skipped, n_failed = enricher.enrich_chunks_jsonl(Path("data/kb/agv_demo/corpora/chunks.jsonl"))
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from dotenv import load_dotenv

from custom_app.services.llm_adapter import (
    GeminiLLMAdapter,
    GeminiServiceUnavailable,
    gemini_response_extract_text,
)

load_dotenv()
_logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一个工业 SOP 知识库的检索增强助手。
我会给你一份完整文档，以及文档中的一个片段（chunk）。
你的任务：用 50-100 字描述这个片段在整个文档中的**位置**和**作用**，
帮助下游检索系统在脱离原文档时仍能定位这个片段。

要求：
1. 输出**纯文本**，不要解释、不要 markdown、不要 JSON
2. 50-100 字，过短信息不足、过长稀释 embedding 信号
3. 重点说明：所属文档主题 / 在哪一步或哪一节 / 上下文衔接（紧接什么、领出什么）
4. 不要复述 chunk 内容本身（embedding 已经有了），只补全语境

示例（输入：AGV 启动手册 STEP 3 chunk）：
本文档介绍 AGV 启动流程共 8 步。STEP 3 紧接电池检查通过之后，是给主控板通电的关键步骤；
完成后将进入 STEP 4 系统自检。"""


USER_TEMPLATE = """<document>
{full_document_text}
</document>

<chunk>
{chunk_contents}
</chunk>

请按要求输出该 chunk 的上下文摘要。"""


_IMG_LINE_RE = re.compile(r"^\[IMG:\s*([^\]]+)\]\s*$")


def _strip_image_placeholders(text: str) -> str:
    """去掉 contents 里的 [IMG: path] 占位行，避免污染 prompt。"""
    if not text:
        return ""
    return "\n".join(
        ln for ln in text.splitlines() if not _IMG_LINE_RE.match(ln.strip())
    ).strip()


@dataclass(frozen=True)
class ContextResult:
    """单条 chunk 的 context 生成结果。"""

    chunk_id: str
    context: str
    error: str | None = None  # 非 None 表示失败，context 应为 ""


class ContextEnricher:
    """批量给 chunks.jsonl 生成 context 字段。

    并发：默认 4 路（与 Gemini 配额匹配）。
    幂等：chunks.jsonl 已有非空 context 的 chunk 自动跳过。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        max_workers: int = 4,
        max_doc_chars: int = 60_000,  # ~30k token 上限保护，避免单文档 prompt 过大
    ) -> None:
        api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get(
            "ULTRARAG_GEMINI_API_KEY"
        )
        if not api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY (or ULTRARAG_GEMINI_API_KEY) is not set"
            )
        # Contextual chunking 用独立 env：默认 gemini-2.0-flash（快、便宜、非 thinking 模型）。
        # 注意：不要复用 ULTRARAG_GEMINI_MODEL —— 那是对话用的，如果是 gemini-3.x thinking
        # 模型，会把 maxOutputTokens 吃在 reasoning 上，导致摘要被截断成 4-14 字。
        model = model or os.environ.get("ULTRARAG_CONTEXTUAL_MODEL", "gemini-2.0-flash")
        self._adapter = GeminiLLMAdapter(api_key=api_key, model=model)
        self._model = model
        self._max_workers = max_workers
        self._max_doc_chars = max_doc_chars

    # ─────────────────────────────────────────────────────────
    # 公共 API
    # ─────────────────────────────────────────────────────────

    def enrich_chunks_jsonl(
        self,
        chunks_path: Path,
        *,
        force: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int, int]:
        """读 chunks.jsonl → 给每个无 context 的 chunk 生成 context → 写回。

        Args:
            chunks_path: chunks.jsonl 路径
            force: True 时忽略已有 context，全部重新生成
            progress_cb: 可选回调，每完成一条调一次 (done, total)

        Returns:
            (n_generated, n_skipped, n_failed)：成功/跳过/失败计数
        """
        if not chunks_path.exists():
            raise FileNotFoundError(f"chunks not found: {chunks_path}")

        rows = [
            json.loads(line)
            for line in chunks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not rows:
            _logger.warning("chunks file is empty: %s", chunks_path)
            return 0, 0, 0

        # 按 doc 聚合 chunk，便于复用「整篇文档文本」给 Gemini
        full_doc_by: dict[str, str] = self._build_full_docs(rows)

        # 选出需要生成的 chunk（未有 context 或 force）
        to_generate: list[tuple[int, dict]] = []
        skipped = 0
        for i, row in enumerate(rows):
            ctx = (row.get("context") or "").strip()
            if ctx and not force:
                skipped += 1
                continue
            to_generate.append((i, row))

        total = len(to_generate)
        if total == 0:
            _logger.info("all chunks already have context; nothing to do")
            return 0, skipped, 0

        _logger.info(
            "enriching %d/%d chunks with context (model=%s, workers=%d)",
            total,
            len(rows),
            self._model,
            self._max_workers,
        )

        results: dict[int, ContextResult] = {}
        done_count = 0
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {}
            for idx, row in to_generate:
                doc_text = full_doc_by.get(row.get("doc") or "", "")
                fut = pool.submit(self._generate_one, row, doc_text)
                futures[fut] = idx
            for fut in as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()
                done_count += 1
                if progress_cb:
                    progress_cb(done_count, total)

        failed = 0
        for idx, res in results.items():
            rows[idx]["context"] = res.context
            if res.error:
                failed += 1
                _logger.warning("context gen failed: %s err=%s", res.chunk_id, res.error)

        # 写回（原子：写临时文件再 rename）
        tmp = chunks_path.with_suffix(chunks_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(chunks_path)

        n_generated = total - failed
        _logger.info(
            "context enrichment done: generated=%d skipped=%d failed=%d",
            n_generated,
            skipped,
            failed,
        )
        return n_generated, skipped, failed

    # ─────────────────────────────────────────────────────────
    # 内部
    # ─────────────────────────────────────────────────────────

    def _build_full_docs(self, rows: Iterable[dict]) -> dict[str, str]:
        """按 doc 字段聚合 chunk → 整篇文档文本（已去图片占位行）。"""
        bucket: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            doc = row.get("doc") or ""
            cid = str(row.get("id") or "")
            body = _strip_image_placeholders(row.get("contents") or "")
            bucket.setdefault(doc, []).append((cid, body))
        out: dict[str, str] = {}
        for doc, items in bucket.items():
            # 保持 chunks.jsonl 原顺序（默认就是文档顺序）
            full = "\n\n".join(b for _, b in items if b)
            if len(full) > self._max_doc_chars:
                _logger.info(
                    "doc %r exceeds %d chars (%d); truncating for prompt safety",
                    doc,
                    self._max_doc_chars,
                    len(full),
                )
                full = full[: self._max_doc_chars]
            out[doc] = full
        return out

    def _generate_one(self, row: dict, full_doc_text: str) -> ContextResult:
        """单 chunk 调 Gemini 生成 context。失败时返回 context=""。"""
        cid = str(row.get("id") or "")
        chunk_body = _strip_image_placeholders(row.get("contents") or "")
        if not chunk_body:
            return ContextResult(chunk_id=cid, context="", error="empty chunk body")
        if not full_doc_text:
            # 单 chunk 文档（如 _intro 短文档）：full_doc = chunk 本身没意义
            # 直接用 chunk_body 作为「文档」，让 Gemini 至少给个主题摘要
            full_doc_text = chunk_body

        user_text = USER_TEMPLATE.format(
            full_document_text=full_doc_text,
            chunk_contents=chunk_body,
        )
        try:
            resp = self._adapter.call(
                messages=[{"role": "user", "content": user_text}],
                system_prompt=SYSTEM_PROMPT,
                generation_config={"temperature": 0.3, "maxOutputTokens": 500},
            )
            text = gemini_response_extract_text(resp).strip()
        except GeminiServiceUnavailable as e:
            return ContextResult(chunk_id=cid, context="", error=f"gemini unavailable: {e}")
        except Exception as e:  # noqa: BLE001 — 任何模型异常都降级
            return ContextResult(chunk_id=cid, context="", error=f"{type(e).__name__}: {e}")

        # 兜底裁剪：50-150 字保护带（防 prompt 失控产长文）
        if len(text) > 300:
            text = text[:300]
        return ContextResult(chunk_id=cid, context=text)
