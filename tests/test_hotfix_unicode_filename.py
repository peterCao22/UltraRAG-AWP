"""
Hotfix TDD：Unicode 安全文件名清理函数

修复：werkzeug.secure_filename 把所有非 ASCII 字符整个删掉，导致中文文件名变成
空主名（"中文.docx" → ".docx"），多个中文文件全部冲突到同一个 doc_id，
索引和上传都只剩一个。

unicode_safe_filename 必须：
- 保留中文、日文、韩文、emoji 等 Unicode 字母数字
- 删除路径分隔符 / \\ 和 .. 防止目录穿越
- 删除控制字符 (\\x00-\\x1f, \\x7f) 和零宽字符
- 保留扩展名
- 空名/全空白名 fallback 到时间戳格式
- 限制总长 ≤ 200 字符（避免文件系统/SQLite 问题）
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 期望函数：custom_app.services.filename_safe.unicode_safe_filename
# ─────────────────────────────────────────────────────────────────────────────

class TestUnicodeSafeFilename:
    def test_module_imports(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        assert callable(unicode_safe_filename)

    def test_chinese_name_preserved(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("智能体测试结果.docx")
        assert out.endswith(".docx")
        assert "智能体" in out
        assert "测试" in out

    def test_english_name_unchanged(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        assert unicode_safe_filename("IFSSOP.docx") == "IFSSOP.docx"
        assert unicode_safe_filename("Battery_Change.pdf") == "Battery_Change.pdf"

    def test_mixed_chinese_english_preserved(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("AGV换电SOP_v2.docx")
        assert "AGV" in out
        assert "换电" in out
        assert "SOP" in out
        assert out.endswith(".docx")

    def test_path_separator_stripped(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("../../etc/passwd.docx")
        assert ".." not in out
        assert "/" not in out
        assert "\\" not in out
        assert out.endswith(".docx")

    def test_backslash_stripped(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        assert "\\" not in unicode_safe_filename("a\\b\\c.docx")

    def test_control_chars_stripped(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        controls = chr(0x00) + chr(0x01) + chr(0x1f) + chr(0x7f)
        out = unicode_safe_filename(f"a{controls[0]}b{controls[1]}c{controls[2]}d{controls[3]}.docx")
        for ch in controls:
            assert ch not in out
        assert out.endswith(".docx")

    def test_zero_width_chars_stripped(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        # ZWSP / ZWNJ / ZWJ
        zw = chr(0x200B) + chr(0x200C) + chr(0x200D)
        out = unicode_safe_filename(f"a{zw[0]}b{zw[1]}c{zw[2]}d.docx")
        for ch in zw:
            assert ch not in out

    def test_empty_string_fallback(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("")
        assert out  # 非空
        # fallback 形式应是某种合理可识别的占位
        assert len(out) > 3

    def test_whitespace_only_fallback(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("   \t\n   ")
        assert out
        assert out.strip() == out

    def test_only_extension_fallback(self):
        """仅有扩展名时（".docx"），主名应得到 fallback。"""
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename(".docx")
        assert out.endswith(".docx")
        # 不应是裸 ".docx"
        assert len(out) > len(".docx")

    def test_multiple_dots_only_last_is_extension(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("v1.2.最终版.docx")
        assert out.endswith(".docx")

    def test_length_capped(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        long_name = "中" * 300 + ".docx"
        out = unicode_safe_filename(long_name)
        # 文件系统通常 255 字节限制；按字节算
        assert len(out.encode("utf-8")) <= 200
        assert out.endswith(".docx")

    def test_collisions_avoidable(self):
        """两个不同的中文名应得到不同的清理结果（不应都被裁成同样的占位）。"""
        from custom_app.services.filename_safe import unicode_safe_filename
        a = unicode_safe_filename("文档A.docx")
        b = unicode_safe_filename("文档B.docx")
        assert a != b

    def test_strips_leading_trailing_dots_and_spaces(self):
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename("  ...spaces.docx...  ")
        # 不应以点或空格开头/结尾（Windows 文件名禁忌）
        assert not out.startswith(".")
        assert not out.startswith(" ")
        assert not out.endswith(" ")
        assert out.endswith(".docx")

    def test_windows_reserved_chars_stripped(self):
        """Windows 不允许 < > : " | ? * 出现在文件名中。"""
        from custom_app.services.filename_safe import unicode_safe_filename
        out = unicode_safe_filename('a<b>c:"d|e?f*.docx')
        for ch in '<>:"|?*':
            assert ch not in out
        assert out.endswith(".docx")


# ─────────────────────────────────────────────────────────────────────────────
# 集成：upload 路由用新函数后，多个中文文件应分别保存
# ─────────────────────────────────────────────────────────────────────────────

class TestUploadKeepsChineseNames:
    @pytest.fixture(autouse=True)
    def _patch_faiss(self, monkeypatch):
        import sys
        import types
        if "faiss" not in sys.modules:
            mod = types.ModuleType("faiss")
            from unittest.mock import MagicMock
            mod.IndexFlatIP = MagicMock()
            mod.read_index = MagicMock()
            mod.write_index = MagicMock()
            monkeypatch.setitem(sys.modules, "faiss", mod)

    def test_upload_two_chinese_files_creates_two_documents(self, tmp_path, monkeypatch):
        """上传两份中文名 docx，db 和磁盘各应留下两条/两份。"""
        # 用临时 DB 隔离
        import custom_app.db as db_module
        db_path = tmp_path / "app.sqlite"
        monkeypatch.setattr(db_module, "DB_PATH", db_path)
        db_module.init_db()

        # 创建 KB 行
        kb_root = tmp_path / "kb_root"
        kb_root.mkdir()
        from custom_app.db import get_conn, now_iso
        ts = now_iso()
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO knowledge_bases
                   (kb_id, name, tenant_id, status, data_path, created_at, updated_at)
                   VALUES ('kb_cn', 'CN', 'default', 'active', ?, ?, ?)""",
                (str(kb_root), ts, ts),
            )

        from custom_app.app import create_app
        client = create_app().test_client()

        # 用 io 模拟两份不同中文名 docx（伪造内容也能存盘）
        import io
        f1 = (io.BytesIO(b"PK\x03\x04 fakedocx 1"), "智能体测试结果A.docx")
        f2 = (io.BytesIO(b"PK\x03\x04 fakedocx 2"), "智能体测试结果B.docx")
        resp = client.post(
            "/api/kb/kb_cn/documents/upload",
            data={"files": [f1, f2]},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()["data"]
        assert body["uploaded"] == 2
        assert len(body["files"]) == 2

        # 文件系统检查：两份不同的真实文件
        raw_dir = kb_root / "raw"
        files = sorted(p.name for p in raw_dir.glob("*.docx"))
        assert len(files) == 2, f"expected 2 distinct files, got {files}"
        # 每个文件名应至少含 A 或 B 的中文片段（保证两个区分得开）
        joined = "".join(files)
        assert "A" in joined or "测试" in joined  # 不强求完全保留，但要可区分
        assert files[0] != files[1]

        # DB 检查：kb_documents 两条
        with get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM kb_documents WHERE kb_id = 'kb_cn'"
            ).fetchone()["c"]
        assert count == 2
