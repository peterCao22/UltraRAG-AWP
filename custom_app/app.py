"""
AGV 私有知识库 Flask 应用入口。

本文件负责创建 Flask app、初始化 SQLite、注册 API 蓝图，并在 Phase 3 中提供
H5 前端页面与本地 vendor 静态资源。

运行方式：
  python -m custom_app.app --port 8080
"""

import hmac
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory

from custom_app.api import chat_bp, kb_bp, roles_bp, sessions_bp
from custom_app.db import init_db
from custom_app.logging_setup import setup_logging


# 文件实现说明：当前文件核心职责是 1) 定位 frontend 目录 -> 2) 初始化 DB 与 API 蓝图 -> 3) 挂载页面入口和静态资源。
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
ADMIN_TOKEN_ENV = "ULTRARAG_ADMIN_TOKEN"
ADMIN_TOKEN_COOKIE = "ultrarag_admin_token"
ADMIN_TOKEN_HEADER = "X-Admin-Token"


def _get_configured_admin_token() -> str:
    """读取可选的管理后台访问 token。

    参数：
        无。

    返回：
        str: 去除首尾空白后的 token；未配置时返回空字符串，表示内网免登录模式。
    """
    return os.getenv(ADMIN_TOKEN_ENV, "").strip()


def _get_request_admin_token() -> str:
    """从当前请求中提取管理后台访问 token。

    参数：
        无。

    返回：
        str: Header 或 Cookie 中提交的 token；API 路径只接受 Header，未提交时返回空字符串。
    """
    header_token = request.headers.get(ADMIN_TOKEN_HEADER, "").strip()
    if _is_api_request(request.path):
        # API 不接受 Cookie 鉴权，避免浏览器自动携带 Cookie 导致 CSRF。
        return header_token

    # 页面请求允许 Cookie，预留给 Phase 4 登录页写入只读会话。
    return (header_token or request.cookies.get(ADMIN_TOKEN_COOKIE, "")).strip()


def _is_admin_request(path: str) -> bool:
    """判断请求路径是否属于管理后台或管理 API。

    参数：
        path: Flask 请求路径。

    返回：
        bool: 是管理后台页面或管理 API 返回 True，否则返回 False。
    """
    return (
        path == "/admin"
        or path.startswith("/admin/")
        or path == "/api/kb"
        or path.startswith("/api/kb/")
        or path == "/api/roles"
        or path.startswith("/api/roles/")
    )


def _is_api_request(path: str) -> bool:
    """判断请求是否为 API 路径。

    参数：
        path: Flask 请求路径。

    返回：
        bool: API 路径返回 True，否则返回 False。
    """
    return path.startswith("/api/")


def create_app() -> Flask:
    """创建并配置 Flask 应用实例。

    参数：
        无。

    返回：
        Flask: 已注册 API 蓝图与 Phase 3 前端路由的应用实例。
    """
    # 必须在任何 logger.info/warning 之前挂 handler，否则那些早期日志会被
    # Python 默认 lastResort handler 吞掉，且永远不会写入 logs/app.log。
    setup_logging()

    app = Flask(
        __name__,
        static_folder=str(FRONTEND_DIR),
        static_url_path="/static",
    )
    if not _get_configured_admin_token():
        logging.getLogger(__name__).warning(
            "%s 未配置，管理后台与管理 API 处于内网免登录模式。",
            ADMIN_TOKEN_ENV,
        )

    init_db()

    # Phase 6.1: 把上一次 Flask 崩溃后残留在 parsing/embedding/indexing/deleting
    # 状态超过 10 分钟的文档标 failed，避免前端轮询永远转圈圈。
    try:
        from custom_app.services.doc_status_recovery import recover_stale_documents
        recover_stale_documents()
    except Exception:
        logging.getLogger(__name__).exception(
            "recover_stale_documents failed at startup; continuing"
        )

    app.register_blueprint(chat_bp)
    app.register_blueprint(kb_bp)
    app.register_blueprint(roles_bp)
    app.register_blueprint(sessions_bp)

    @app.before_request
    def require_admin_token():
        """在配置 token 时保护管理后台页面。

        参数：
            无。

        返回：
            Response | None: 未授权时返回登录页重定向；通过或未启用保护时返回 None。
        """
        configured_token = _get_configured_admin_token()
        if not configured_token or not _is_admin_request(request.path):
            return None

        request_token = _get_request_admin_token()
        if hmac.compare_digest(request_token, configured_token):
            return None

        if _is_api_request(request.path):
            return jsonify({"success": False, "error": "unauthorized"}), 401

        # 页面请求统一跳转 Phase 4 预留登录页，避免泄露管理页 HTML 内容。
        return redirect("/login")

    @app.after_request
    def add_security_headers(response):
        """为所有响应追加基础安全头。

        参数：
            response: Flask 即将返回给浏览器的响应对象。

        返回：
            Response: 已追加安全头的响应对象。
        """
        # 内网环境也关闭 MIME sniff，避免静态资源或上传内容被浏览器误判为可执行内容。
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'"
        )
        return response

    @app.route('/')
    @app.route('/chat')
    def index():
        """返回对话主页入口。

        参数：
            无。

        返回：
            Response: `custom_app/frontend/index.html` 的静态文件响应。
        """
        resp = make_response(send_from_directory(app.static_folder, 'index.html'))
        # 避免浏览器长期使用缓存的 Sprint 旧版 HTML（侧栏文案等不更新）。
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    @app.route('/admin')
    @app.route('/admin/')
    def admin():
        """返回管理后台入口。

        参数：
            无。

        返回：
            Response: `custom_app/frontend/admin.html` 的静态文件响应。
        """
        return send_from_directory(app.static_folder, 'admin.html')

    @app.route('/login')
    @app.route('/login/')
    def login():
        """返回 Phase 4 预留登录页入口。

        参数：
            无。

        返回：
            Response: `custom_app/frontend/login.html` 的静态文件响应。
        """
        return send_from_directory(app.static_folder, 'login.html')

    @app.route('/frontend/<path:filename>')
    def frontend_static(filename: str):
        """返回 Phase 3 前端根目录下的静态资源。

        参数：
            filename: 相对 `frontend/` 的文件路径。

        返回：
            Response: 对应静态文件响应。
        """
        return send_from_directory(app.static_folder, filename)

    @app.route('/static/js/<path:path>')
    def send_js(path):
        """返回旧版 `/static/js/*` 资源，供 Sprint 迁移期兼容。

        参数：
            path: 相对 `frontend/js/` 的文件路径。

        返回：
            Response: 对应静态文件响应。
        """
        return send_from_directory(FRONTEND_DIR / 'js', path)

    @app.route('/static/css/<path:path>')
    def send_css(path):
        """返回旧版 `/static/css/*` 资源，供 Sprint 迁移期兼容。

        参数：
            path: 相对 `frontend/css/` 的文件路径。

        返回：
            Response: 对应静态文件响应。
        """
        return send_from_directory(FRONTEND_DIR / 'css', path)

    # 绝对路径：与 __file__ 同级的项目根下 data/kb
    KB_BASE = Path(__file__).resolve().parent.parent / "data" / "kb"

    @app.route('/images/<path:img_path>')
    def kb_image(img_path: str):
        """提供知识库图片静态资源。

        在所有知识库目录中查找 images/<img_path>，返回首个命中的文件。
        URL 格式：/images/<doc>/<filename>
        磁盘路径：data/kb/<kb_id>/images/<doc>/<filename>
        """
        if not KB_BASE.is_dir():
            return jsonify({"error": "kb base not found"}), 404
        for kb_dir in sorted(KB_BASE.iterdir()):
            if not kb_dir.is_dir():
                continue
            candidate = kb_dir / "images" / img_path
            if candidate.exists():
                return send_from_directory(str(kb_dir / "images"), img_path)
        return jsonify({"error": "image not found"}), 404

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
