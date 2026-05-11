from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import quote

# 解析 docx_parser 写入的内联图片占位 [IMG: 相对路径]
_INLINE_IMG_RE = re.compile(r"\[IMG:\s*([^\]]+)\]")


def _to_image_url(rel_path: str) -> str:
    """把存储里的相对路径（如 'images/IFS 系统培训手册/img_0001.png'）
    转成对前端可直接 src 的 URL：'/images/IFS%20%E7%B3%BB%E7%BB%9F.../img_0001.png'。

    保留 '/' 不编码，对空格、中文等做 URL 编码。LLM 把这个字段值原样填到
    `![](URL)` 里就能正常被 markdown 解析（无空格）+ 浏览器请求（已编码）。
    """
    s = (rel_path or "").strip()
    if not s:
        return ""
    # 去掉可能的前导 'images/'，统一加 /images/ 前缀
    if s.startswith("images/"):
        s = s[len("images/"):]
    return "/images/" + quote(s, safe="/")


def _inline_img_to_markdown(text: str) -> str:
    """把 [IMG: 相对路径] 占位替换为 markdown ![](已编码 URL)，每张图独占一行。

    - 占位前后插入空行，让 marked 把图片解析为块级元素
    - URL 编码确保浏览器能正常加载（中文/空格都转 %xx）
    """
    if not text or "[IMG:" not in text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        rel = (m.group(1) or "").strip()
        url = _to_image_url(rel)
        # 前后空行让 markdown 当作块级图片
        return f"\n\n![]({url})\n\n"

    return _INLINE_IMG_RE.sub(_sub, text)


class ListChunksTool:
    """Deep Read 工具：按 doc 名称返回该文档的全部 chunk，供 Agent 精读完整 SOP。"""

    name = "list_knowledge_chunks"

    openai_schema: Dict[str, Any] = {
        "type": "function",
        "function": {
            "name": "list_knowledge_chunks",
            "description": (
                "获取指定文档的全部分块内容，用于深度阅读完整步骤。"
                "返回字段说明：每个 chunk 含 image_urls 字段（已 URL 编码，可直接放入 Markdown）。"
                "**强制规则**：当某个步骤的 chunk 含 image_urls 时，必须在该步骤文字下方"
                "**单独一行**写 ![](URL)，URL 直接复制 image_urls 数组里的字符串，"
                "**不得修改、不得拼接、不得在同一行放多个图片**。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {
                        "type": "string",
                        "description": "文档名称（如 IFSSOP），从 knowledge_search 结果的 doc 字段获取",
                    },
                },
                "required": ["doc_id"],
            },
        },
    }

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def run(self, *, doc_id: str) -> List[Dict[str, Any]]:
        doc = (doc_id or "").strip()
        if not doc:
            return []
        result = []
        for row in self._rows:
            if str(row.get("doc", "")).strip() != doc:
                continue
            raw_images = list(row.get("images", []) or [])
            raw_contents = row.get("contents", "")
            # 把 [IMG: ...] 内联占位替换为 markdown ![](已编码 URL)，
            # 让 LLM 看到的就是"步骤说明 → 图片 → 下一步骤"的真实顺序，
            # 不必自己挑哪张图配哪段文字。
            rendered = _inline_img_to_markdown(raw_contents)
            result.append({
                "id": row.get("id", ""),
                "title": row.get("title", ""),
                "contents": rendered,
                "doc": row.get("doc", ""),
                # 保留原 images 字段做兼容；image_urls 仍提供供老 prompt 路径使用
                "images": raw_images,
                "image_urls": [_to_image_url(p) for p in raw_images],
            })
        return result
