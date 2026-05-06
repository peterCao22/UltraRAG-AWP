import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import faiss
import requests
import yaml
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

from custom_app.services.google_embedder import embed_query

logger = logging.getLogger(__name__)

# 文件实现说明：
# - 该文件提供 AGV 场景的单轮 RAG 执行器，负责把“用户问题”转换为可追溯答案。
# - 核心流程顺序：
#   1) init() 读取检索/生成配置，并加载 chunks 与 FAISS 索引；
#   2) chat() 对问题做 embedding 后执行向量检索，得到命中文档片段；
#   3) 使用 Jinja 模板组装提示词并调用本地 vLLM 生成答案；
#   4) 将命中证据结构化为 sources，并生成可前端渲染的 answer_blocks（含图文）。
IMAGES_MARK = "\n[IMAGES]\n"

# 判断用户是否在问「流程 / 步骤 / 更换」类问题，用于决定是否对同一 doc 做全量 SOP 扩展
_PROCEDURE_INTENT_RE = re.compile(
    r"步骤|流程|操作|更换|怎么|如何|怎样|SOP|procedure|steps?|how\s+to|sequence|battery|电池|换电|充电",
    re.IGNORECASE,
)
_STEP_IN_ID_RE = re.compile(r"_step_(\d+)", re.IGNORECASE)
_STEP_IN_TITLE_RE = re.compile(r"STEP\s*(\d+)", re.IGNORECASE)
_EXCERPT_DELIM_RE = re.compile(
    r"^<<<\s*EXCERPT\s*(\d+)\s*>>>\s*$", re.MULTILINE
)
# LLM 用于标记与问题无关章节的占位符（与 agv_qa_rag.jinja 约定一致）
_SKIP_MARKER_RE = re.compile(r"^\s*\[跳过\]\s*$")


def answer_blocks_to_display_markdown(
    blocks: List[Dict[str, Any]], fallback_plain: str = ""
) -> str:
    """
    将 answer_blocks 拼成一段 Markdown：中文小节与插图 data URL 交替，供主气泡与 SSE chunk 使用。

    参数:
        blocks: _answer_to_blocks 产出的块列表。
        fallback_plain: 无块或块拼出为空时退回的纯文本（通常为 _compose_answer_text 结果）。
    返回:
        str: Markdown 字符串。
    """
    if not blocks:
        return (fallback_plain or "").strip()
    parts: List[str] = []
    for b in blocks:
        typ = b.get("type")
        if typ == "text":
            c = (b.get("content") or "").strip()
            if c:
                parts.append(c)
        elif typ == "image":
            url = (b.get("data_url") or "").strip()
            if not url:
                continue
            title = (b.get("title") or "").replace("\n", " ").strip() or "SOP 插图"
            alt = title.replace("]", "").replace("[", "")[:120]
            parts.append(f"![{alt}]({url})")
    merged = "\n\n".join(parts)
    if merged.strip():
        return merged
    return (fallback_plain or "").strip()


def sources_citation_only_for_ui(
    sources: List[Dict[str, Any]],
    *,
    note: str = "（步骤与插图见上方助手回复）",
) -> List[Dict[str, Any]]:
    """
    正文已内嵌插图时，折叠「引用来源」里仅保留可追溯字段，去掉英文长摘录与重复缩略图。

    参数:
        sources: 完整 sources 列表。
        note: 替代 snippet/excerpt 的短说明（中文）。
    返回:
        新列表（浅拷贝字段，不修改入参对象）。
    """
    out: List[Dict[str, Any]] = []
    for s in sources:
        out.append(
            {
                "source_id": s.get("source_id"),
                "doc": s.get("doc"),
                "title": s.get("title"),
                "display_title": s.get("display_title"),
                "snippet": note,
                "excerpt": note,
                "images": [],
            }
        )
    return out


class RagRunner:
    """
    AGV Phase-1 RAG 运行器：Google 查询向量 + FAISS 检索 + vLLM 生成。

    参数:
        无（类实例参数由 __init__ 提供）。
    返回:
        无（通过实例方法提供初始化与问答能力）。
    """

    def __init__(
        self,
        kb_id: str = "agv_demo",
        kb_base_dir: str = "data/kb",
        prompt_dir: str = "prompt",
        retriever_param_path: str = "servers/retriever/parameter.yaml", # 用来读取检索侧配置（比如 top_k）
        generation_param_path: str = "servers/generation/parameter.yaml", # 用来读取生成侧配置（比如模型名、base_url、采样参数等）
    ) -> None:
        """
        初始化运行器基础路径与运行时状态。

        参数:
            kb_id: 知识库 ID，对应 data/kb 下的子目录名。
            kb_base_dir: 知识库根目录。
            prompt_dir: Prompt 模板目录。
            retriever_param_path: 检索配置文件路径。
            generation_param_path: 生成配置文件路径。
        返回:
            None
        """
        load_dotenv()
        self.kb_id = kb_id
        self.kb_base_dir = Path(kb_base_dir)
        self.prompt_dir = Path(prompt_dir)
        self.retriever_param_path = Path(retriever_param_path)
        self.generation_param_path = Path(generation_param_path)

        self._index = None
        self._rows: List[Dict[str, Any]] = []
        self._top_k = 8
        self._recall_top_k = 12
        # final_top_k<=0：不截断召回结果，保证 SOP 全步骤可见
        self._final_top_k = 0
        self._chat_cfg: Dict[str, Any] = {}
        self._rerank_cfg: Dict[str, Any] = {}
        self._rerank_model = None
        self._rerank_load_error: Optional[str] = None
        self._rerank_resolved_device: Optional[str] = None

    def _kb_dir(self) -> Path:
        """
        计算当前知识库目录路径。

        参数:
            无。
        返回:
            Path: 当前知识库目录（data/kb/<kb_id>）。
        """
        # 当前知识库目录：data/kb/<kb_id>
        return self.kb_base_dir / self.kb_id

    def _load_yaml(self, path: Path) -> Dict[str, Any]:
        """
        读取 YAML 配置并返回字典。

        参数:
            path: YAML 文件路径。
        返回:
            Dict[str, Any]: 解析后的配置字典，空文件返回空字典。
        """
        # 统一用 utf-8 读取配置，避免 Windows 默认编码问题
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def _split_contents_and_images(self, contents: str) -> tuple[str, List[str]]:
        """
        拆分 chunk 内容中的正文与图片路径列表。

        参数:
            contents: 原始内容字符串，可能包含 [IMAGES] 区块。
        返回:
            tuple[str, List[str]]: (正文文本, 图片相对路径列表)。
        """
        # chunks.jsonl 中 contents 末尾可能拼接 [IMAGES] 区块，这里拆正文和图片路径
        raw = contents or ""
        if IMAGES_MARK not in raw:
            return raw.strip(), []
        # 通过IMAGES_MARK分割，text_part是正文部分，image_part是图片路径部分
        text_part, image_part = raw.split(IMAGES_MARK, 1)
        # 按换行切断，获取多张图片路径
        images = [ln.strip() for ln in image_part.splitlines() if ln.strip()]
        return text_part.strip(), images

    def _image_path_to_data_url(self, img_rel: str) -> str:
        """
        将图片相对路径转换为 data URL（base64）。

        参数:
            img_rel: 图片相对当前知识库目录的路径。
        返回:
            str: 可直接用于前端 img 的 data URL；文件不存在时返回空字符串。
        """
        # 相对路径 -> data URL(base64)，方便前端直接渲染 <img src="">
        img_abs = self._kb_dir() / img_rel
        if not img_abs.exists():
            return ""
        suffix = img_abs.suffix.lower()
        mime = "image/png"
        if suffix in [".jpg", ".jpeg"]:
            mime = "image/jpeg"
        elif suffix == ".webp":
            mime = "image/webp"
        raw = img_abs.read_bytes()
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _build_sources(self, ids: List[int]) -> List[Dict[str, Any]]:
        """
        根据检索命中 ID 构建可审计证据列表。

        参数:
            ids: 检索命中行号列表。
        返回:
            List[Dict[str, Any]]: 每条包含 title/snippet/images 的证据对象。
        """
        # 审计/溯源输出：每个命中片段附 title/snippet/images
        sources: List[Dict[str, Any]] = []
        for idx in ids:
            if idx < 0 or idx >= len(self._rows):
                continue
            # _rows是一个字典列表，每个字典包含id/title/contents/doc/images等字段
            row = self._rows[idx]
            plain_text, image_paths = self._split_contents_and_images(
                row.get("contents", "")
            )
            images_b64 = []
            for p in image_paths:
                data_url = self._image_path_to_data_url(p)
                if data_url:
                    images_b64.append(data_url)
            excerpt = plain_text.strip()
            if len(excerpt) > 15000:
                excerpt = excerpt[:15000] + "\n…"
            sources.append(
                {
                    "source_id": str(row.get("id", idx)),
                    "doc": row.get("doc", ""),
                    "title": row.get("title", ""),
                    "display_title": self._format_display_heading(row),
                    "snippet": excerpt[:400],
                    "excerpt": excerpt,
                    "images": images_b64,
                }
            )
        return sources

    @staticmethod
    def _parse_step_number(row: Dict[str, Any]) -> Optional[int]:
        """从 chunk 解析 STEP 序号；非步骤块（如 intro）返回 None。"""
        m = _STEP_IN_TITLE_RE.search(row.get("title", "") or "")
        if m:
            return int(m.group(1))
        m2 = _STEP_IN_ID_RE.search(str(row.get("id", "")))
        if m2:
            return int(m2.group(1))
        return None

    def _is_step_chunk_row(self, row: Dict[str, Any]) -> bool:
        """是否为带编号的步骤块（用于判断是否需要按 doc 扩展整份 SOP）。"""
        return self._parse_step_number(row) is not None

    @staticmethod
    def _procedure_intent(question: str) -> bool:
        return bool(_PROCEDURE_INTENT_RE.search(question or ""))

    def _docs_for_agent_deep_read(self, hit_ids: List[int]) -> Set[str]:
        """
        智能推理（层 A）：对首轮向量命中中出现的、带 doc 归属的文档做「全文 chunk」扩展。

        与 `_docs_to_expand` 不同：不依赖流程类意图或步骤块命中，只要命中里出现 doc 即纳入候选，
        再经 `_narrow_expand_docs` 收敛为主文档，避免多 SOP 同时灌入上下文。
        """
        out: Set[str] = set()
        for i in hit_ids:
            if i < 0 or i >= len(self._rows):
                continue
            d = self._rows[i].get("doc")
            if d is None:
                continue
            ds = str(d).strip()
            if ds:
                out.add(ds)
        return out

    def _docs_to_expand(
        self, hit_ids: List[int], question: str
    ) -> Set[str]:
        """
        根据首轮命中与用户意图，决定要扩展为「全文」的 doc 集合。

        规则：
        - 若命中里出现某 doc 的步骤块，则扩展该 doc（补全 STEP 1..N，避免向量漏召回中间步）。
        - 若用户明显在问流程，则对命中中出现的、且语料里含步骤块的 doc 也扩展（避免只命中 intro）。
        """
        docs_from_steps: Set[str] = set()
        docs_from_hits: Set[str] = set()
        for i in hit_ids:
            if i < 0 or i >= len(self._rows):
                continue
            row = self._rows[i]
            d = row.get("doc")
            if not d:
                continue
            docs_from_hits.add(str(d))
            if self._is_step_chunk_row(row):
                docs_from_steps.add(str(d))
        if docs_from_steps:
            return docs_from_steps
        if self._procedure_intent(question):
            out: Set[str] = set()
            for d in docs_from_hits:
                if any(
                    self._is_step_chunk_row(r) and str(r.get("doc")) == d
                    for r in self._rows
                ):
                    out.add(d)
            return out
        return set()

    def _doc_first_seen_order(self, hit_ids: List[int]) -> List[str]:
        order: List[str] = []
        seen: Set[str] = set()
        for i in hit_ids:
            if i < 0 or i >= len(self._rows):
                continue
            d = self._rows[i].get("doc")
            if not d:
                continue
            ds = str(d)
            if ds not in seen:
                seen.add(ds)
                order.append(ds)
        return order

    def _narrow_expand_docs(
        self, hit_ids: List[int], expand_docs: Set[str]
    ) -> Set[str]:
        """
        多个 doc 同时可扩展时，只保留「与本次检索最相关」的一份，避免把无关 SOP 整本拼进来。

        策略：在首轮 hit_ids 中按 doc 计数，命中条数最多的 doc 作为主文档；平局时取在检索结果中更早出现的 doc。
        """
        if len(expand_docs) <= 1:
            return expand_docs
        counts: Dict[str, int] = {}
        first_rank: Dict[str, int] = {}
        for rank, i in enumerate(hit_ids):
            if i < 0 or i >= len(self._rows):
                continue
            d = str(self._rows[i].get("doc", ""))
            if d not in expand_docs:
                continue
            counts[d] = counts.get(d, 0) + 1
            if d not in first_rank:
                first_rank[d] = rank
        if not counts:
            return expand_docs
        primary = sorted(
            counts.keys(),
            key=lambda d: (-counts[d], first_rank.get(d, 999)),
        )[0]
        return {primary}

    def _format_display_heading(self, row: Dict[str, Any]) -> str:
        """
        面向前端的章节标题：步骤用「第 N 步」，避免重复冗长的「Doc | STEP N」技术标题。
        """
        step = self._parse_step_number(row)
        if step is not None:
            return f"第 {step} 步"
        doc = (row.get("doc") or "").strip()
        title = (row.get("title") or "").strip()
        prefix = f"{doc} | " if doc else ""
        if prefix and title.startswith(prefix):
            tail = title[len(prefix) :].strip()
            if tail:
                title = tail
        if doc and title == doc:
            return "流程说明"
        return title if title else "说明"

    def _expand_hit_ids(
        self,
        hit_ids: List[int],
        question: str,
        *,
        agent_mode: str = "quick",
    ) -> Tuple[List[int], List[str]]:
        """
        将向量命中扩展为「同一 SOP 文档」下的全部 chunk，并按 intro → STEP1→STEP2… 排序。

        参数:
            agent_mode: ``quick`` 时沿用意图/步骤启发式扩展；``agent`` 时强制对命中 doc 做全文扩展（层 A）。

        返回:
            (扩展并排序后的行号列表, 被扩展的 doc 名列表)
        """
        if not hit_ids:
            return [], []
        mode = (agent_mode or "quick").strip().lower()
        if mode not in ("quick", "agent"):
            mode = "quick"
        if mode == "agent":
            expand_docs = self._docs_for_agent_deep_read(hit_ids)
        else:
            expand_docs = self._docs_to_expand(hit_ids, question)
        if not expand_docs:
            return list(hit_ids), []
        expand_docs = self._narrow_expand_docs(hit_ids, expand_docs)
        # 只保留主文档下的全部 chunk，不要把首轮检索里其它 doc 的碎片并进来（否则会混入 Alarm 等无关 SOP）
        all_idx: Set[int] = set()
        for j, row in enumerate(self._rows):
            if str(row.get("doc", "")) in expand_docs:
                all_idx.add(j)
        doc_order = [d for d in self._doc_first_seen_order(hit_ids) if d in expand_docs]
        for d in sorted(expand_docs):
            if d not in doc_order:
                doc_order.append(d)
        rank = {d: i for i, d in enumerate(doc_order)}

        def sort_key(j: int) -> Tuple[int, int, int, str]:
            row = self._rows[j]
            d = str(row.get("doc", ""))
            step = self._parse_step_number(row)
            if step is not None:
                return (rank.get(d, 999), 1, step, str(row.get("id", "")))
            return (rank.get(d, 999), 0, 0, str(row.get("id", "")))

        ordered = sorted(all_idx, key=sort_key)
        return ordered, sorted(expand_docs)

    def _parse_excerpt_sections(self, answer: str) -> Dict[int, str]:
        """解析模型输出中的 <<<EXCERPT n>>> 分段。"""
        text = answer or ""
        matches = list(_EXCERPT_DELIM_RE.finditer(text))
        if not matches:
            return {}
        out: Dict[int, str] = {}
        for i, m in enumerate(matches):
            k = int(m.group(1))
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            out[k] = text[start:end].strip()
        return out

    def _compose_answer_text(
        self, raw: str, parsed: Dict[int, str], n_excerpts: int
    ) -> str:
        """
        将各节译文拼接为可读答案，过滤掉 LLM 标记为 [跳过] 的无关节。
        若有效节齐全则返回拼接结果；否则保留模型原文便于人工核对。
        """
        raw = (raw or "").strip()
        if n_excerpts <= 0:
            return raw
        kept: List[str] = []
        for k in range(1, n_excerpts + 1):
            sec = (parsed.get(k) or "").strip()
            if sec and not self._is_skip_section(sec):
                kept.append(sec)
        if kept:
            return "\n\n".join(kept)
        return raw

    def _answer_declares_no_information(self, answer_raw: str) -> bool:
        """
        判断模型是否声明「根据现有文档无法回答」（与 agv_qa_rag.jinja 拒答句式对齐）。

        此时不应再挂载检索命中的插图，避免正文拒答与无关 SOP 截图并存。
        """
        s = answer_raw or ""
        if "根据现有文档" in s and (
            "未找到与该问题相关的信息" in s or "无法回答" in s
        ):
            return True
        return False

    def _no_information_display_text(
        self, answer_raw: str, answer_plain: str
    ) -> str:
        """从拒答类输出中抽取可读正文，优先显式拒答句，其次退回 compose 结果。"""
        keep: List[str] = []
        for line in (answer_raw or "").splitlines():
            ls = line.strip()
            if not ls:
                continue
            if "根据现有文档" in ls and ("未找到" in ls or "无法回答" in ls):
                keep.append(ls)
            elif "文档中未找到足够相关信息" in ls and "无法回答" in ls:
                keep.append(ls)
        if keep:
            return "\n\n".join(dict.fromkeys(keep))
        ap = (answer_plain or "").strip()
        if ap:
            return ap
        return (answer_raw or "").strip()[:4000]

    @staticmethod
    def _is_skip_section(text: str) -> bool:
        """检测 LLM 是否将该节标记为 [跳过]（与问题无关，不渲染文字与图片）。"""
        return bool(_SKIP_MARKER_RE.match((text or "").strip()))

    def _section_text_allows_images(self, section_text: str) -> bool:
        """
        本节展示文本是否应挂载原文图片。

        模板规定的「未找到信息 / 无法回答」及「模型未按 EXCERPT 格式」占位段不挂图，
        避免与正文语义矛盾；有实质译文的节仍按节在文字下挂图。
        """
        t = (section_text or "").strip()
        if not t:
            return False
        if self._is_skip_section(t):
            return False
        if "（模型未按分段格式" in t:
            return False
        if "未找到与该问题相关的信息" in t:
            return False
        if "文档中未找到足够相关信息" in t:
            return False
        if "没有足够信息回答" in t:
            return False
        if "无法回答" in t and len(t) < 360:
            return False
        return True

    def _answer_to_blocks(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
        answer_plain: str = "",
    ) -> List[Dict[str, Any]]:
        """
        按摘录顺序生成块：每个 chunk 一段译文，紧接着仅挂载该 chunk 原文中的图片（与正文严格同源性）。

        参数:
            answer: 模型输出（含 <<<EXCERPT n>>> 为佳）。
            sources: 与 passages 顺序一致的证据列表。
            answer_plain: ``_compose_answer_text`` 结果；用于全局拒答时折叠展示。
        返回:
            图文交替块列表。
        """
        blocks: List[Dict[str, Any]] = []
        if not sources and (answer or "").strip():
            blocks.append({"type": "text", "content": (answer or "").strip()})
            return blocks

        # 全局「文档无法回答该问题」：不再按节重复、不挂任何插图（用户侧栏仍可见 sources 元数据）。
        if sources and self._answer_declares_no_information(answer):
            collapse = self._no_information_display_text(answer, answer_plain)
            return [{"type": "text", "content": collapse}]

        parsed = self._parse_excerpt_sections(answer)
        for idx, src in enumerate(sources):
            k = idx + 1
            heading = (src.get("display_title") or src.get("title") or "").strip() or (
                "(untitled)"
            )
            title_audit = (src.get("title") or "").strip() or heading
            zh = (parsed.get(k) or "").strip()
            # LLM 标记为与问题无关时，跳过该节的文字和图片
            if self._is_skip_section(zh):
                continue
            if not zh:
                excerpt = (src.get("excerpt") or "").strip()
                if len(excerpt) > 1200:
                    excerpt = excerpt[:1200] + "\n…"
                sid = (src.get("source_id") or "").strip()
                sid_note = f" 溯源：`{sid}`" if sid else ""
                # 用「章节标题」说明缺段，避免 EXCERPT 序号与界面「第 n 步」不一致造成误解
                zh = (
                    "（模型未按分段格式输出本节「%s」%s；以下为知识库原文摘录，便于核对）\n\n%s"
                    % (heading, sid_note, excerpt)
                )
            source_id = src.get("source_id", "")
            blocks.append({"type": "text", "source_id": source_id, "content": f"### {heading}\n\n{zh}"})
            if self._section_text_allows_images(zh):
                for img_idx, data_url in enumerate(src.get("images") or []):
                    blocks.append(
                        {
                            "type": "image",
                            "source_id": source_id,
                            "source_idx": idx,
                            "image_idx": img_idx,
                            "title": title_audit,
                            "data_url": data_url,
                        }
                    )
        return blocks

    def _build_prompt(self, question: str, ids: List[int]) -> str:
        """
        按模板渲染最终 prompt 文本。

        参数:
            question: 用户问题。
            ids: 检索命中行号列表。
        返回:
            str: 渲染后的提示词文本。
        """
        # 用 prompt/agv_qa_rag.jinja 渲染最终提问文本
        env = Environment(loader=FileSystemLoader(str(self.prompt_dir)))
        tmpl = env.get_template("agv_qa_rag.jinja")
        passages = []
        for idx in ids:
            if idx < 0 or idx >= len(self._rows):
                continue
            row = self._rows[idx]
            plain_text, _ = self._split_contents_and_images(row.get("contents", ""))
            passages.append({
                "title": row.get("title", ""),
                "contents": plain_text,
                "source_id": str(row.get("id", idx)),
            })
        return tmpl.render(question=question, passages=passages)

    def _gemini_rest_base(self) -> str:
        """Google AI（Generative Language API）根 URL；Vertex 等可换 ULTRARAG_GEMINI_API_HOST。"""
        return (
            os.environ.get("ULTRARAG_GEMINI_API_HOST", "https://generativelanguage.googleapis.com")
            .strip()
            .rstrip("/")
        )

    def _gemini_api_key(self) -> str:
        """Gemini 文本生成与 embedding 共用同一类 API Key（AI Studio / Google AI）。"""
        return (
            os.environ.get("ULTRARAG_GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )

    def _gemini_model_id(self) -> str:
        """
        Gemini ``models/{id}`` 中的 id。优先环境变量；否则若 yaml 的 model_name 以 gemini 开头则沿用；
        否则默认 ``gemini-2.0-flash``（与 OpenAI 兼容段里的 Qwen 名区分）。
        """
        env_m = os.environ.get("ULTRARAG_GEMINI_MODEL", "").strip()
        if env_m:
            return env_m
        yaml_m = (self._chat_cfg.get("model_name") or "").strip()
        if yaml_m.lower().startswith("gemini"):
            return yaml_m
        return "gemini-2.0-flash"

    def _gemini_generation_config(self) -> Dict[str, Any]:
        """将 _chat_cfg 采样参数映射为 Gemini generationConfig（camelCase）。"""
        try:
            max_out = int(self._chat_cfg.get("max_tokens", 4096))
        except (TypeError, ValueError):
            max_out = 4096
        max_out = max(256, min(max_out, 8192))
        return {
            "temperature": float(self._chat_cfg.get("temperature", 0.2)),
            "topP": float(self._chat_cfg.get("top_p", 0.9)),
            "maxOutputTokens": max_out,
        }

    @staticmethod
    def _gemini_text_from_response_obj(obj: Dict[str, Any]) -> str:
        """从 generateContent 或 stream 单帧 JSON 中拼接文本 parts。"""
        parts_out: List[str] = []
        for cand in obj.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            content = cand.get("content") or {}
            for p in content.get("parts") or []:
                if isinstance(p, dict):
                    t = p.get("text")
                    if isinstance(t, str) and t:
                        parts_out.append(t)
        return "".join(parts_out)

    def _generate_openai(self, prompt_text: str) -> str:
        """调用 OpenAI 兼容 ``/chat/completions``（vLLM 等）。"""
        base_url = self._chat_cfg["base_url"].rstrip("/")
        payload = {
            "model": self._chat_cfg["model_name"],
            "messages": [
                {"role": "system", "content": self._chat_cfg["system_prompt"]},
                {"role": "user", "content": prompt_text},
            ],
            "temperature": self._chat_cfg["temperature"],
            "top_p": self._chat_cfg["top_p"],
            "max_tokens": self._chat_cfg["max_tokens"],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._chat_cfg['api_key']}",
        }
        timeout = int(self._chat_cfg.get("request_timeout_sec", 300))
        resp = requests.post(
            f"{base_url}/chat/completions", headers=headers, json=payload, timeout=timeout
        )
        if not resp.ok:
            raise RuntimeError(f"vLLM generation failed {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"vLLM generation empty choices: {str(data)[:400]}")
        msg = choices[0].get("message") or {}
        raw_content = msg.get("content")
        if raw_content is None:
            logger.warning(
                "vLLM message.content is null (finish_reason=%s keys=%s)",
                choices[0].get("finish_reason"),
                list(msg.keys()),
            )
            return ""
        if isinstance(raw_content, list):
            parts: List[str] = []
            for part in raw_content:
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(part, str):
                    parts.append(part)
            return "".join(parts).strip()
        return str(raw_content).strip()

    def _generate_gemini(self, prompt_text: str) -> str:
        """
        调用 Google Gemini ``:generateContent`` REST（与 embedding 同源 API Key）。

        参数:
            prompt_text: 用户侧完整 prompt（system 走 ``systemInstruction``）。
        返回:
            str: 模型文本。
        异常:
            RuntimeError: 未配置 Key、HTTP 错误或响应无正文时抛出。
        """
        key = self._gemini_api_key()
        if not key:
            raise RuntimeError(
                "使用 Gemini 生成需在 .env 配置 GOOGLE_API_KEY 或 ULTRARAG_GEMINI_API_KEY"
            )
        model = self._gemini_model_id()
        base = self._gemini_rest_base()
        url = f"{base}/v1beta/models/{model}:generateContent"
        sys_t = (self._chat_cfg.get("system_prompt") or "").strip()
        body: Dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": (prompt_text or "").strip()}]},
            ],
            "generationConfig": self._gemini_generation_config(),
        }
        if sys_t:
            body["systemInstruction"] = {"parts": [{"text": sys_t}]}
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        timeout = int(self._chat_cfg.get("request_timeout_sec", 300))
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        # Google 响应常无 charset；requests 可能按 ISO-8859-1 误读 UTF-8，导致中文乱码。
        resp.encoding = "utf-8"
        if not resp.ok:
            raise RuntimeError(f"Gemini generateContent failed {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        pf = data.get("promptFeedback") or {}
        br = pf.get("blockReason")
        if br:
            raise RuntimeError(f"Gemini prompt blocked: {br}")
        text = self._gemini_text_from_response_obj(data).strip()
        if not text:
            raise RuntimeError(f"Gemini empty text: {str(data)[:400]}")
        return text

    def _generate(self, prompt_text: str) -> str:
        """
        生成答案：按 ``_chat_cfg['backend']`` 选择 OpenAI 兼容端或 Gemini。

        参数:
            prompt_text: 渲染后的 prompt 文本。
        返回:
            str: 模型输出文本（去除首尾空白）。
        异常:
            RuntimeError: 远端错误或无正文。
        """
        backend = (self._chat_cfg.get("backend") or "openai").strip().lower()
        if backend == "gemini":
            return self._generate_gemini(prompt_text)
        return self._generate_openai(prompt_text)

    def _generate_stream_openai(self, prompt_text: str) -> Iterator[str]:
        """OpenAI 兼容流式 ``/chat/completions``。"""
        base_url = self._chat_cfg["base_url"].rstrip("/")
        payload = {
            "model": self._chat_cfg["model_name"],
            "messages": [
                {"role": "system", "content": self._chat_cfg["system_prompt"]},
                {"role": "user", "content": prompt_text},
            ],
            "temperature": self._chat_cfg["temperature"],
            "top_p": self._chat_cfg["top_p"],
            "max_tokens": self._chat_cfg["max_tokens"],
            "stream": True,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._chat_cfg['api_key']}",
        }
        timeout = int(self._chat_cfg.get("request_timeout_sec", 300))
        with requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        ) as resp:
            if not resp.ok:
                raise RuntimeError(
                    f"vLLM generation failed {resp.status_code}: {resp.text[:300]}"
                )
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        break
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                err = obj.get("error")
                if err:
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(msg or "vLLM stream error")
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if not isinstance(delta, dict):
                    continue
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    yield piece

    def _generate_stream_gemini(self, prompt_text: str) -> Iterator[str]:
        """
        Gemini ``:streamGenerateContent?alt=sse``，按 SSE ``data:`` 帧 yield 增量文本。

        参数:
            prompt_text: 与 ``_generate_gemini`` 相同。
        返回:
            Iterator[str]: 各帧解析出的文本片段。
        异常:
            RuntimeError: Key 缺失、HTTP 错误、或 error 字段。
        """
        key = self._gemini_api_key()
        if not key:
            raise RuntimeError(
                "使用 Gemini 生成需在 .env 配置 GOOGLE_API_KEY 或 ULTRARAG_GEMINI_API_KEY"
            )
        model = self._gemini_model_id()
        base = self._gemini_rest_base()
        url = f"{base}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        sys_t = (self._chat_cfg.get("system_prompt") or "").strip()
        body: Dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": (prompt_text or "").strip()}]},
            ],
            "generationConfig": self._gemini_generation_config(),
        }
        if sys_t:
            body["systemInstruction"] = {"parts": [{"text": sys_t}]}
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        timeout = int(self._chat_cfg.get("request_timeout_sec", 300))
        with requests.post(
            url, headers=headers, json=body, stream=True, timeout=timeout
        ) as resp:
            resp.encoding = "utf-8"
            if not resp.ok:
                raise RuntimeError(
                    f"Gemini streamGenerateContent failed {resp.status_code}: {resp.text[:400]}"
                )
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw == "[DONE]":
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                err = obj.get("error")
                if err:
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise RuntimeError(msg or "Gemini stream error")
                pf = obj.get("promptFeedback") or {}
                br = pf.get("blockReason")
                if br:
                    raise RuntimeError(f"Gemini prompt blocked: {br}")
                piece = self._gemini_text_from_response_obj(obj)
                if piece:
                    yield piece

    def _generate_stream(self, prompt_text: str) -> Iterator[str]:
        """
        流式生成：OpenAI 兼容或 Gemini SSE。

        参数:
            prompt_text: 用户侧完整 prompt。
        返回:
            Iterator[str]: 文本增量。
        """
        backend = (self._chat_cfg.get("backend") or "openai").strip().lower()
        if backend == "gemini":
            yield from self._generate_stream_gemini(prompt_text)
        else:
            yield from self._generate_stream_openai(prompt_text)

    def _rewrite_query(self, question: str) -> str:
        """
        轻量 Query Rewrite：将用户问题改写为更适合检索的短查询。

        参数:
            question: 原始用户问题。
        返回:
            str: 改写后的查询；失败时返回原问题。
        """
        q = (question or "").strip()
        if not q:
            return q

        rewrite_prompt = (
            "You are a query rewriter for technical retrieval.\n"
            "Rewrite the user question into ONE concise search query.\n"
            "Rules:\n"
            "- Keep original intent and key constraints.\n"
            "- Keep AGV domain terms and technical nouns.\n"
            "- For SOP/procedure questions, keep words like steps, procedure, sequence, battery replacement if relevant.\n"
            "- Do not answer the question.\n"
            "- Output only the rewritten query text.\n\n"
            f"User question: {q}"
        )
        try:
            rewritten = self._generate(rewrite_prompt).strip()
        except Exception:
            return q
        if not rewritten:
            return q
        # 避免模型返回多行解释，仅取首行作为查询
        return rewritten.splitlines()[0].strip() or q

    def _resolve_rerank_device(self, preferred: str) -> str:
        """解析 rag_rerank.device：auto 时优先 CUDA（若 torch 可用）。"""
        pref = (preferred or "auto").strip().lower()
        if pref == "auto":
            try:
                import torch

                return "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                return "cpu"
        return pref

    def _ensure_rerank_model(self) -> Any:
        """
        懒加载 CrossEncoder；失败时记录 _rerank_load_error，不抛异常。

        返回:
            CrossEncoder 实例，或 None（未启用 / 依赖缺失 / 加载失败）。
        """
        if self._rerank_model is not None:
            return self._rerank_model
        if not (self._rerank_cfg or {}).get("enabled"):
            return None
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            self._rerank_load_error = f"sentence_transformers: {e}"
            return None
        model_path = (self._rerank_cfg or {}).get("model_name_or_path") or ""
        if not model_path:
            self._rerank_load_error = "model_name_or_path empty"
            return None
        device = self._resolve_rerank_device(str(self._rerank_cfg.get("device", "auto")))
        trust = bool(self._rerank_cfg.get("trust_remote_code", True))
        try:
            self._rerank_model = CrossEncoder(
                model_path, device=device, trust_remote_code=trust
            )
            self._rerank_resolved_device = device
            self._rerank_load_error = None
        except Exception as e:
            if device == "cuda":
                try:
                    self._rerank_model = CrossEncoder(
                        model_path, device="cpu", trust_remote_code=trust
                    )
                    self._rerank_resolved_device = "cpu"
                    self._rerank_load_error = None
                    logger.warning("rag_rerank: CUDA load failed (%s), fallback to CPU", e)
                except Exception as e2:
                    self._rerank_load_error = f"cuda:{e}; cpu:{e2}"
                    self._rerank_model = None
            else:
                self._rerank_load_error = str(e)
                self._rerank_model = None
        return self._rerank_model

    def _rerank_hit_ids(
        self, query: str, hit_ids: List[int]
    ) -> Tuple[List[int], Dict[str, Any]]:
        """
        对 FAISS 命中列表按 CrossEncoder 分数重排（在 SOP 扩展与截断之前执行）。

        参数:
            query: 用于打分的查询文本（与检索一致，使用 rewrite 后的查询）。
            hit_ids: 向量检索得到的行号列表。

        返回:
            (重排后的 hit_ids, 可并入 meta 的统计信息)。
        """
        info: Dict[str, Any] = {
            "rerank_applied": False,
            "rerank_ms": 0,
            "rerank_skip_reason": None,
            "rerank_device": None,
        }
        if not hit_ids:
            info["rerank_skip_reason"] = "empty_hits"
            return hit_ids, info
        if not (self._rerank_cfg or {}).get("enabled"):
            info["rerank_skip_reason"] = "disabled"
            return hit_ids, info
        t0 = time.perf_counter()
        model = self._ensure_rerank_model()
        if model is None:
            info["rerank_skip_reason"] = self._rerank_load_error or "model_unavailable"
            info["rerank_ms"] = int((time.perf_counter() - t0) * 1000)
            return hit_ids, info
        pairs: List[List[str]] = []
        for i in hit_ids:
            if i < 0 or i >= len(self._rows):
                info["rerank_skip_reason"] = "invalid_hit_index"
                info["rerank_ms"] = int((time.perf_counter() - t0) * 1000)
                return hit_ids, info
            row = self._rows[i]
            plain, _ = self._split_contents_and_images(row.get("contents", ""))
            blob = f"{row.get('title', '')}\n{plain}".strip()[:2000]
            pairs.append([query, blob])
        batch_size = int(self._rerank_cfg.get("batch_size", 8))
        try:
            scores = model.predict(
                pairs, batch_size=max(1, batch_size), show_progress_bar=False
            )
        except Exception as e:
            info["rerank_skip_reason"] = f"predict_failed:{e}"
            info["rerank_ms"] = int((time.perf_counter() - t0) * 1000)
            return hit_ids, info
        order = sorted(
            range(len(hit_ids)), key=lambda j: float(scores[j]), reverse=True
        )
        reranked = [hit_ids[j] for j in order]
        info["rerank_applied"] = True
        info["rerank_ms"] = int((time.perf_counter() - t0) * 1000)
        info["rerank_device"] = self._rerank_resolved_device
        logger.info(
            "rag_rerank applied device=%s ms=%s n=%s",
            info["rerank_device"],
            info["rerank_ms"],
            len(reranked),
        )
        return reranked, info

    def init(self) -> None:
        """
        启动初始化（一次）：
        1) 读取 retriever / generation 配置
        2) 加载 chunks.jsonl 到内存
        3) 加载 FAISS 索引
        4) 组装生成模型参数

        参数:
            无。
        返回:
            None
        异常:
            FileNotFoundError: chunks 文件或索引文件缺失时抛出。
        """
        retr_cfg = self._load_yaml(self.retriever_param_path)
        gen_cfg = self._load_yaml(self.generation_param_path)

        # 语料文件：按行存放 chunk（JSONL），检索命中索引后用它回查原文/标题/图片等元数据，用于组装 prompt 与 sources。
        corpus_path = self._kb_dir() / "corpora" / "chunks.jsonl"
        # 向量索引文件：FAISS 索引本体，保存每个 chunk 的向量；查询向量在这里做相似度检索，返回命中下标。
        index_path = self._kb_dir() / "index" / "index.index"
        # 控制每次检索从 FAISS 取回多少条候选片段
        self._top_k = int(retr_cfg.get("top_k", 8))
        self._recall_top_k = int(retr_cfg.get("recall_top_k", 12))
        self._final_top_k = int(retr_cfg.get("final_top_k", 0))
        self._rerank_cfg = dict(retr_cfg.get("rag_rerank") or {})
        # 重排模型首次加载常需数分钟；可用环境变量覆盖 yaml（内网排障优先关 rerank）。
        _rd = os.environ.get("ULTRARAG_DISABLE_RERANK", "").strip().lower() in ("1", "true", "yes")
        _re = os.environ.get("ULTRARAG_ENABLE_RERANK", "").strip().lower() in ("1", "true", "yes")
        if _rd:
            self._rerank_cfg["enabled"] = False
            logger.warning("rag_rerank: disabled via ULTRARAG_DISABLE_RERANK")
        elif _re:
            self._rerank_cfg["enabled"] = True
            logger.info("rag_rerank: enabled via ULTRARAG_ENABLE_RERANK")
        self._rerank_model = None
        self._rerank_load_error = None
        self._rerank_resolved_device = None

        if not corpus_path.exists():
            raise FileNotFoundError(f"chunks file not found: {corpus_path}")
        if not index_path.exists():
            raise FileNotFoundError(f"faiss index not found: {index_path}")

        # 加载语料文件：按行解析 JSONL，每行是 chunk（JSON 对象），_rows 包含 id/title/contents/doc/images 等字段。
        self._rows = [
            json.loads(line)
            for line in corpus_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # 加载 FAISS 索引：读取二进制文件，得到向量索引本体。
        self._index = faiss.read_index(str(index_path))

        openai_cfg = (gen_cfg.get("backend_configs") or {}).get("openai") or {}
        sampling = gen_cfg.get("sampling_params") or {}

        # 单次 HTTP 读超时（秒）：长上下文 + 大 max_tokens 时 vLLM 可能超过 120s，见 openai.request_timeout_sec
        try:
            _to = int(openai_cfg.get("request_timeout_sec", 300))
        except (TypeError, ValueError):
            _to = 300
        _to = max(30, min(_to, 7200))

        _yaml_backend = (gen_cfg.get("chat_backend") or "openai").strip().lower()
        if _yaml_backend not in ("openai", "gemini"):
            _yaml_backend = "openai"

        # _chat_cfg：backend=openai 走 vLLM/OpenAI 兼容；backend=gemini 走 Google AI generateContent。
        self._chat_cfg = {
            "backend": _yaml_backend,
            "model_name": openai_cfg.get("model_name", "gpt-oss-120b"),
            "base_url": openai_cfg.get("base_url", "http://192.168.8.44:8000/v1"),
            "api_key": openai_cfg.get("api_key", "dummy"),
            "system_prompt": gen_cfg.get("system_prompt", ""),
            "temperature": sampling.get("temperature", 0.2),
            "top_p": sampling.get("top_p", 0.9),
            "max_tokens": sampling.get("max_tokens", 1024),
            "request_timeout_sec": _to,
        }
        self._apply_ultrarag_generation_env_overrides()
        if (self._chat_cfg.get("backend") or "").strip().lower() == "gemini":
            logger.info(
                "RagRunner generation: backend=gemini model=%s",
                self._gemini_model_id(),
            )
        else:
            logger.info(
                "RagRunner generation: backend=openai model=%s base_url=%s",
                self._chat_cfg.get("model_name"),
                self._chat_cfg.get("base_url"),
            )

    def _apply_ultrarag_generation_env_overrides(self) -> None:
        """
        用环境变量覆盖 ``servers/generation/parameter.yaml`` 中的 OpenAI 兼容端点。

        便于在 .env 中切换内网 vLLM / 其它网关而无需改 yaml；将来也可由后端写入进程环境
        或页面「模型管理」落库后再注入（与 WeKnora 类似）。

        变量（非空时生效）:
            ULTRARAG_CHAT_BACKEND: ``openai`` | ``gemini``（Gemini 用 ``GOOGLE_API_KEY`` 等，见 ``_generate_gemini``）。
            ULTRARAG_OPENAI_BASE_URL: 如 ``http://192.168.8.44:8800/v1``（勿漏 ``/v1``）。
            ULTRARAG_OPENAI_MODEL: 与 vLLM ``--served-model-name`` 一致。
            ULTRARAG_OPENAI_API_KEY: 可选；内网 vLLM 常仍填任意非空即可。
            ULTRARAG_OPENAI_REQUEST_TIMEOUT_SEC: 可选，整数秒，范围 30–7200。

        返回:
            None
        """
        cfg = self._chat_cfg
        if not cfg:
            return
        applied: List[str] = []
        be = os.environ.get("ULTRARAG_CHAT_BACKEND", "").strip().lower()
        if be in ("openai", "gemini"):
            cfg["backend"] = be
            applied.append("chat_backend")
        base = os.environ.get("ULTRARAG_OPENAI_BASE_URL", "").strip()
        if base:
            cfg["base_url"] = base.rstrip("/")
            applied.append("base_url")
        model = os.environ.get("ULTRARAG_OPENAI_MODEL", "").strip()
        if model:
            cfg["model_name"] = model
            applied.append("model_name")
        key = os.environ.get("ULTRARAG_OPENAI_API_KEY", "").strip()
        if key:
            cfg["api_key"] = key
            applied.append("api_key")
        raw_to = os.environ.get("ULTRARAG_OPENAI_REQUEST_TIMEOUT_SEC", "").strip()
        if raw_to:
            try:
                t = max(30, min(int(raw_to), 7200))
                cfg["request_timeout_sec"] = t
                applied.append("request_timeout_sec")
            except (TypeError, ValueError):
                logger.warning(
                    "ULTRARAG_OPENAI_REQUEST_TIMEOUT_SEC ignored (not int): %r", raw_to
                )
        if applied:
            logger.info(
                "generation config overridden from env: %s → backend=%s model=%s base_url=%s",
                ", ".join(applied),
                cfg.get("backend"),
                cfg.get("model_name"),
                cfg.get("base_url"),
            )

    def _normalize_agent_mode(self, agent_mode: str | None) -> str:
        """将 API 传入的 agent_mode 规范为 quick 或 agent。"""
        m = (agent_mode or "quick").strip().lower()
        return m if m in ("quick", "agent") else "quick"

    def _prepare_chat_context(
        self,
        question: str,
        top_k: int | None = None,
        *,
        agent_mode: str = "quick",
    ) -> Dict[str, Any]:
        """
        检索与 prompt 组装（与 `chat` 前半段一致），供同步生成与流式生成共用。

        参数:
            question: 用户问题。
            top_k: 可选覆盖检索条数。
            agent_mode: ``quick`` | ``agent``；agent 时启用层 A 全文 chunk 扩展（见 `_expand_hit_ids`）。
        返回:
            dict: 含 q、rewritten_q、hit_ids、prompt_text、rerank_meta、expanded_docs、
            recall_k、final_k、final_k_cfg 等下游 `_build_result_from_raw` 所需字段。
        """
        if self._index is None:
            raise RuntimeError("RagRunner not initialized. Call init() first.")
        q = (question or "").strip()
        if not q:
            raise ValueError("question is empty")

        requested_mode = self._normalize_agent_mode(agent_mode)

        rewritten_q = self._rewrite_query(q)
        q_vec = embed_query(rewritten_q).astype("float32").reshape(1, -1)
        recall_k = int(self._recall_top_k)
        if top_k is not None:
            recall_k = int(top_k)
        if top_k is None and self._top_k:
            recall_k = max(recall_k, int(self._top_k))
        recall_k = max(1, min(recall_k, len(self._rows)))
        _, indices = self._index.search(q_vec, recall_k)
        hit_ids = [int(x) for x in indices[0].tolist() if int(x) >= 0]
        hit_ids, rerank_meta = self._rerank_hit_ids(rewritten_q, hit_ids)
        final_k_cfg = int(self._final_top_k)
        if final_k_cfg > 0:
            final_k = max(1, min(final_k_cfg, len(hit_ids)))
            hit_ids = hit_ids[:final_k]
        hit_ids, expanded_docs = self._expand_hit_ids(
            hit_ids, q, agent_mode=requested_mode
        )
        final_k = len(hit_ids)
        prompt_text = self._build_prompt(q, hit_ids)
        effective_mode = (
            "agent"
            if (requested_mode == "agent" and bool(expanded_docs))
            else "quick"
        )
        degraded = requested_mode == "agent" and not expanded_docs
        degrade_reason = (
            "no_doc_metadata_in_retrieval_hits"
            if degraded
            else None
        )
        return {
            "q": q,
            "rewritten_q": rewritten_q,
            "hit_ids": hit_ids,
            "prompt_text": prompt_text,
            "rerank_meta": rerank_meta,
            "expanded_docs": expanded_docs,
            "recall_k": recall_k,
            "final_k": final_k,
            "final_k_cfg": final_k_cfg,
            "requested_agent_mode": requested_mode,
            "effective_agent_mode": effective_mode,
            "degraded": degraded,
            "degrade_reason": degrade_reason,
        }

    def _build_result_from_raw(
        self, prep: Dict[str, Any], answer_raw: str
    ) -> Dict[str, Any]:
        """
        将模型原始输出与检索上下文拼成与 `chat` 一致的返回结构（answer / sources / meta）。

        参数:
            prep: `_prepare_chat_context` 的返回值。
            answer_raw: 模型生成的原始字符串（非展示 Markdown）。
        """
        hit_ids: List[int] = prep["hit_ids"]
        rewritten_q = prep["rewritten_q"]
        rerank_meta = prep["rerank_meta"]
        expanded_docs = prep["expanded_docs"]
        recall_k = prep["recall_k"]
        final_k = prep["final_k"]
        final_k_cfg = prep["final_k_cfg"]

        sources = self._build_sources(hit_ids)
        parsed_sections = self._parse_excerpt_sections(answer_raw)
        answer_plain = self._compose_answer_text(
            answer_raw, parsed_sections, len(sources)
        )
        answer_blocks = self._answer_to_blocks(answer_raw, sources, answer_plain)
        has_inline_images = any(b.get("type") == "image" for b in answer_blocks)
        display_answer = answer_blocks_to_display_markdown(
            answer_blocks, answer_plain
        )
        n_sources = len(sources)
        no_hit_declared = self._answer_declares_no_information(answer_raw)
        # 无命中/拒答：不向前端返回检索片段，避免「无法回答」下仍展示无关引用与缩略图。
        if no_hit_declared:
            sources_out: List[Dict[str, Any]] = []
            sources_omitted = bool(n_sources)
        elif has_inline_images and sources:
            sources_out = []
            sources_omitted = bool(n_sources)
        else:
            sources_out = sources
            sources_omitted = False
        meta_out: Dict[str, Any] = {
            "recall_top_k": recall_k,
            "final_top_k": final_k,
            "truncated": final_k_cfg > 0,
            "expanded_docs": expanded_docs,
            "primary_expanded_doc": expanded_docs[0] if expanded_docs else None,
            "sop_full_doc_expand": bool(expanded_docs),
            "retrieval_source_count": n_sources,
            "sources_omitted_for_ui": sources_omitted,
            **rerank_meta,
        }
        if no_hit_declared:
            meta_out["no_answer_from_documents"] = True
        if "effective_agent_mode" in prep:
            meta_out["effective_agent_mode"] = prep["effective_agent_mode"]
        if "degraded" in prep:
            meta_out["degraded"] = bool(prep["degraded"])
        if prep.get("degrade_reason"):
            meta_out["degrade_reason"] = prep["degrade_reason"]
        return {
            "answer": display_answer,
            "answer_blocks": answer_blocks,
            "sources": sources_out,
            "rewrite_query": rewritten_q,
            "meta": meta_out,
        }

    def chat(
        self,
        question: str,
        top_k: int | None = None,
        *,
        agent_mode: str = "quick",
    ) -> Dict[str, Any]:
        """
        单轮问答主流程：
        question -> query embedding -> FAISS 检索 -> Prompt -> LLM 生成 -> 结构化输出

        参数:
            question: 用户输入问题。
            top_k: 可选覆盖默认检索条数；为 None 时使用配置值。
            agent_mode: ``quick`` | ``agent``；与流式接口语义一致（层 A / 降级 meta）。
        返回:
            Dict[str, Any]: 含 answer、answer_blocks、sources。
            answer 为展示用 Markdown（由 answer_blocks 拼接，含 data URL 插图，与中文步骤同屏）；
            当存在内嵌插图、或模型声明「根据现有文档无法回答」时，sources 返回空列表（前端不展示「引用来源」），
            条数见 meta.retrieval_source_count；meta.sources_omitted_for_ui 为 True；拒答时另有 meta.no_answer_from_documents。
        异常:
            RuntimeError: 未初始化索引时抛出。
            ValueError: 问题为空时抛出。
        """
        prep = self._prepare_chat_context(question, top_k, agent_mode=agent_mode)
        answer_raw = self._generate(prep["prompt_text"])
        return self._build_result_from_raw(prep, answer_raw)

    # ──────────────────────────────────────────────────────────────
    # 阶段 A：agent 模式模拟推理步骤 SSE（接口与阶段 B ReAct 兼容）
    # ──────────────────────────────────────────────────────────────

    def _format_tool_hint(self, tool_name: str, args: dict) -> str:
        """生成用户友好的工具调用描述，不暴露内部函数名。"""
        q = str(args.get("query") or args.get("keywords") or "")[:40]
        doc = str(args.get("doc_id") or args.get("doc") or "")[:40]
        hints: dict[str, str] = {
            "knowledge_search": f'搜索知识库："{q}"' if q else "搜索知识库",
            "keyword_search": f'文本搜索："{q}"' if q else "文本搜索",
            "list_knowledge_chunks": f"阅读文档：《{doc}》" if doc else "阅读文档完整内容",
            "final_answer": "提交最终答案",
        }
        return hints.get(tool_name, tool_name)

    def _emit_agent_reasoning_steps(
        self, prep: dict, question: str
    ) -> Iterator[Dict[str, Any]]:
        """
        阶段 A：在 agent 模式下，在 LLM 生成前发射模拟的推理步骤 SSE 事件。

        事件序列（与阶段 B ReAct 引擎输出格式完全一致，前端无需改造）：
          thought → tool_call → tool_result → thought → tool_call → tool_result → thought
        """
        hit_ids: List[int] = prep.get("hit_ids") or []
        expanded_docs: List[str] = prep.get("expanded_docs") or []
        n_hits = len(hit_ids)

        # ── Step 1: 意图评估 + 语义搜索 ──
        yield {"type": "thought", "content": "分析问题，确定检索策略，准备搜索知识库…"}

        t0 = time.perf_counter()
        search_args = {"query": question[:40]}
        yield {
            "type": "tool_call",
            "tool_name": "knowledge_search",
            "hint": self._format_tool_hint("knowledge_search", search_args),
        }
        search_ms = int((time.perf_counter() - t0) * 1000)
        yield {
            "type": "tool_result",
            "tool_name": "knowledge_search",
            "summary": f"找到 {n_hits} 个相关片段" if n_hits else "未找到相关片段",
            "duration_ms": max(search_ms, 1),
        }

        # ── Step 2: Deep Read 全文阅读（仅当有扩展文档时）──
        if expanded_docs:
            primary_doc = expanded_docs[0]
            yield {
                "type": "thought",
                "content": f"对检索结果进行深度阅读，获取《{primary_doc}》的完整内容…",
            }
            t1 = time.perf_counter()
            read_args = {"doc_id": primary_doc}
            yield {
                "type": "tool_call",
                "tool_name": "list_knowledge_chunks",
                "hint": self._format_tool_hint("list_knowledge_chunks", read_args),
            }
            read_ms = int((time.perf_counter() - t1) * 1000)
            n_expanded = len(hit_ids)
            yield {
                "type": "tool_result",
                "tool_name": "list_knowledge_chunks",
                "summary": f"已读取 {n_expanded} 个分块，包含完整步骤内容",
                "duration_ms": max(read_ms, 1),
            }

        # ── Step 3: 综合分析 ──
        yield {"type": "thought", "content": "综合所有检索内容，准备生成最终答案…"}

    def chat_stream(
        self,
        question: str,
        top_k: int | None = None,
        *,
        agent_mode: str = "quick",
        profile: bool = False,
    ) -> Iterator[Dict[str, Any]]:
        """
        以 SSE 事件字典序列的形式输出单轮问答结果。

        说明:
            生成阶段调用 OpenAI 兼容接口的 ``stream: true``，按服务端推送的 delta
            多次产出 ``chunk``；检索与 query rewrite 仍在首段 status 之后同步完成。
            流结束后由 ``_build_result_from_raw`` 得到插图与 Markdown，经 ``done`` 前
            的 ``meta`` / ``sources`` 与前端 ``onDone`` 对齐展示。

        参数:
            question: 用户问题。
            top_k: 可选覆盖检索条数，语义同 chat()。
            agent_mode: ``quick`` | ``agent``；agent 为层 A 全文扩展，无法扩展时 ``meta`` 内含降级标记。
            profile: 为 True 时在 ``type=meta`` 事件中附加 ``phase_timings_ms``（毫秒），
                用于 Phase P 端到端延迟排查（prepare / 首 token / 生成总时长）。

        返回:
            Iterator[Dict[str, Any]]: 每条为 {"type": ..., ...} 的事件字典。

        异常:
            与 chat() 相同（如未 init、问题为空等）。
        """
        normalized_mode = self._normalize_agent_mode(agent_mode)
        chat_backend = "unknown"
        if hasattr(self, "_chat_cfg") and self._chat_cfg:
            chat_backend = (self._chat_cfg.get("backend") or "openai").strip().lower()
        logger.info(
            "chat_stream kb_id=%s agent_mode=%s backend=%s",
            self.kb_id,
            normalized_mode,
            chat_backend,
        )
        yield {"type": "status", "content": "正在检索并生成回答…"}
        t_prep_begin = time.perf_counter()
        prep = self._prepare_chat_context(question, top_k, agent_mode=normalized_mode)
        t_prep_end = time.perf_counter()

        # ── 阶段 A：agent 模式发射模拟推理步骤 SSE（接口与阶段 B ReAct 完全兼容）──
        if normalized_mode == "agent" and not prep.get("degraded"):
            yield from self._emit_agent_reasoning_steps(prep, question)

        yield {"type": "status", "content": "正在生成回答…"}
        t_gen_begin = time.perf_counter()
        pieces: List[str] = []
        first_chunk = True
        t_first_chunk: Optional[float] = None
        for piece in self._generate_stream(prep["prompt_text"]):
            if first_chunk:
                t_first_chunk = time.perf_counter()
                first_chunk = False
            pieces.append(piece)
            yield {"type": "chunk", "content": piece}
        t_gen_end = time.perf_counter()
        answer_raw = "".join(pieces).strip()
        result = self._build_result_from_raw(prep, answer_raw)
        sources = result.get("sources") or []
        if sources:
            yield {"type": "sources", "sources": sources}
        meta_event: Dict[str, Any] = {
            "type": "meta",
            "kb_id": self.kb_id,
            "rewrite_query": result.get("rewrite_query"),
            "meta": result.get("meta"),
        }
        inner_meta = result.get("meta") or {}
        if inner_meta.get("degraded") or prep.get("degraded"):
            meta_event["degraded"] = True
            meta_event["degraded_reason"] = (
                prep.get("degrade_reason") or "no_documents_matched"
            )
            meta_event["message"] = (
                "命中片段缺少文档归属，无法做全文深度阅读，已按快速问答生成。"
            )
        if profile:
            prep_ms = round((t_prep_end - t_prep_begin) * 1000, 2)
            gen_total_ms = round((t_gen_end - t_gen_begin) * 1000, 2)
            first_ms = (
                round((t_first_chunk - t_gen_begin) * 1000, 2)
                if t_first_chunk is not None
                else None
            )
            meta_event["phase_timings_ms"] = {
                "prepare_context_ms": prep_ms,
                "first_token_ms": first_ms,
                "generate_stream_total_ms": gen_total_ms,
                "server_active_ms": round((t_gen_end - t_prep_begin) * 1000, 2),
            }
        yield meta_event
        yield {"type": "done", "answer": result.get("answer", "")}
