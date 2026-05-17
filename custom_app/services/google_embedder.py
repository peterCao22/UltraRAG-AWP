"""
Google Gemini Embedding 适配器
调用原生 API（非 OpenAI 兼容格式），产出 embedding.npy

模型: gemini-embedding-001
  - 稳定版，纯文本，默认 3072 维，可截断到 768/1536/3072
  - task_type=RETRIEVAL_DOCUMENT（建库）/ RETRIEVAL_QUERY（检索）

官方文档: https://ai.google.dev/gemini-api/docs/embeddings
"""
import json
import os
import sys
import time
from pathlib import Path
from typing import List

import numpy as np
import requests
from dotenv import load_dotenv

# Must match suffix appended by docx_parser.pack_chunk
IMAGES_MARK = "\n[IMAGES]\n"

load_dotenv()

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768          # 截断到 768 维，节省存储，质量接近 3072
EMBED_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:batchEmbedContents"
BATCH_SIZE = 20          # 保守批次，避免触发速率限制
RATE_DELAY = 0.1         # 批次间延迟（秒）


def embed_texts(texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> np.ndarray:
    """
    批量将文本列表转换为向量矩阵。

    Args:
        texts: 文本列表
        task_type: "RETRIEVAL_DOCUMENT"（建库）或 "RETRIEVAL_QUERY"（检索）

    Returns:
        shape (N, EMBED_DIM) 的 float32 ndarray，已 L2 归一化
    """
    if not GOOGLE_API_KEY or GOOGLE_API_KEY.startswith("请填入"):
        raise ValueError("GOOGLE_API_KEY 未配置，请在 .env 文件中填入有效的 API Key")

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GOOGLE_API_KEY,
    }

    all_embeddings = []
    total = len(texts)

    for start in range(0, total, BATCH_SIZE):
        batch = texts[start: start + BATCH_SIZE]
        payload = {
            "requests": [
                {
                    "model": f"models/{EMBED_MODEL}",
                    "content": {"parts": [{"text": t}]},
                    "taskType": task_type,
                    "outputDimensionality": EMBED_DIM,
                }
                for t in batch
            ]
        }

        for attempt in range(3):
            try:
                resp = requests.post(EMBED_URL, json=payload, headers=headers, timeout=30)
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise RuntimeError(f"网络请求失败: {e}") from e
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429:
                wait = 2 ** attempt * 2
                print(f"  速率限制 (429)，等待 {wait}s...")
                time.sleep(wait)
                continue

            if not resp.ok:
                raise RuntimeError(f"API 请求失败 {resp.status_code}: {resp.text[:200]}")

            break

        data = resp.json()
        for item in data["embeddings"]:
            vec = np.array(item["values"], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            all_embeddings.append(vec)

        done = min(start + BATCH_SIZE, total)
        print(f"  Embedding 进度: {done}/{total}")
        time.sleep(RATE_DELAY)

    return np.array(all_embeddings, dtype=np.float32)


def embed_query(query: str) -> np.ndarray:
    """单条查询向量（RETRIEVAL_QUERY task），供问答时检索用。"""
    return embed_texts([query], task_type="RETRIEVAL_QUERY")[0]


def strip_images_footer(contents: str) -> str:
    """Remove file path footer from chunk text before embedding."""
    if IMAGES_MARK in contents:
        return contents.split(IMAGES_MARK, 1)[0].strip()
    return (contents or "").strip()


def compose_doc_embedding_text(row: dict) -> str:
    """构造一条 chunk 的嵌入输入文本。

    Phase 4.3：heading_path 增强
        当 chunk 含 structure.heading_path 时（Phase 4+ schema），将标题层级链
        以 "A > B > C" 形式作为前缀拼接，强化父级标题语义。

    Phase 8.2.1：Contextual chunking
        当 chunk 含非空 `context` 字段时（由 services/chunking/contextual.py 生成），
        在 heading_path 与 title 之前再加一行文档级上下文摘要，帮助 embedding 在
        chunk 脱离原文档时仍能定位。缺失 context 时退化到 Phase 4.3 行为，零回归。

    格式（按优先级从外到内）：
        [context]
        A > B > C   ← heading_path
        <title>
        <contents>
    """
    structure = row.get("structure") or {}
    heading_path = structure.get("heading_path") or []
    title = row.get("title", "") or ""
    body = strip_images_footer(row.get("contents", ""))
    context = (row.get("context") or "").strip()

    parts: list[str] = []
    if context:
        parts.append(context)
    if heading_path:
        # heading_path 可能是 list 或 tuple；过滤空串
        cleaned = [str(h).strip() for h in heading_path if str(h).strip()]
        if cleaned:
            parts.append(" > ".join(cleaned))
    if title:
        parts.append(title)
    if body:
        parts.append(body)
    return "\n".join(parts).strip()


def build_embedding_npy(chunks_jsonl: str, output_npy: str) -> None:
    """
    读取 chunks.jsonl，对 chunk 批量 embedding，保存为 .npy。
    供建索引流程替换 UltraRAG 的 retriever_embed 步骤。

    Phase 4.3：嵌入输入由 compose_doc_embedding_text() 统一构造，
    含 structure.heading_path 的 chunk 会获得标题层级链前缀增强。

    Args:
        chunks_jsonl: chunks.jsonl 文件路径
        output_npy:   输出的 embedding.npy 路径
    """
    rows = [
        json.loads(line)
        for line in Path(chunks_jsonl).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 不向量化图片路径行，仅对正文 + 标题（+ heading_path 前缀）做 embedding
    texts = [compose_doc_embedding_text(r) for r in rows]

    print(f"开始 embedding {len(texts)} 条 chunks ...")
    embeddings = embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")

    Path(output_npy).parent.mkdir(parents=True, exist_ok=True)
    np.save(output_npy, embeddings)
    print(f"已保存: {output_npy}，shape={embeddings.shape}")


if __name__ == "__main__":
    # 快速连通性测试
    print(f"使用模型: {EMBED_MODEL}，维度: {EMBED_DIM}")
    vecs = embed_texts(["AGV 换电步骤", "电池规格参数"], task_type="RETRIEVAL_DOCUMENT")
    print(f"测试通过，返回 shape: {vecs.shape}")
    print(f"第一个向量前 5 维: {vecs[0][:5]}")
