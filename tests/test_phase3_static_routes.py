"""
Phase 3 静态页面路由测试。

本文件验证 Flask 在 Phase 3 中同时承担 API 与前端静态资源服务：
- 输入：测试客户端请求页面入口、前端资源与既有 API。
- 输出：断言响应状态、页面内容与 MIME 类型符合前端部署约定。
- 运行方式：pytest tests/test_phase3_static_routes.py -v

注意：本文件会 stub ``custom_app.api`` 并 ``del`` 后重载 ``custom_app.app``。
若不在用例结束后弹出 ``custom_app.app``，同进程内后续用例（如 ``TestChatStreamSse``）
会继续使用已注册「空 chat 蓝图」的 app 缓存，导致 ``/api/chat/stream`` 404。
"""

import sys

import pytest
from flask import Blueprint, jsonify


@pytest.fixture(autouse=True)
def _unload_custom_app_after_phase3_stub_test():
    """每个用例结束后丢弃 ``custom_app.app`` 模块缓存，避免污染其它测试文件。"""
    yield
    sys.modules.pop("custom_app.app", None)


# 文件实现说明：当前文件只覆盖 Flask 路由契约，流程为 1) 创建测试 app -> 2) 请求页面或资源 -> 3) 校验页面入口不会遮蔽 API。
def _create_test_app(tmp_path, monkeypatch):
    """创建 Flask 测试应用，并隔离测试时产生的 SQLite 与知识库目录。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        Flask: 已开启 TESTING 的应用实例。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "data" / "kb").mkdir(parents=True)

    chat_bp = Blueprint("chat_api_test", __name__)
    kb_bp = Blueprint("kb_api_test", __name__)
    roles_bp = Blueprint("roles_api_test", __name__)
    sessions_bp = Blueprint("sessions_api_test", __name__)

    @sessions_bp.route("/api/sessions", methods=["GET", "POST"])
    def sessions_stub():
        """占位：静态路由测试不验证会话 API 行为。"""
        return jsonify({"success": True, "data": {"items": []}})

    @kb_bp.route("/api/kb/", methods=["GET"])
    @kb_bp.route("/api/kb", methods=["GET"])
    def list_kb():
        """返回最小知识库列表响应，用于验证前端路由不会遮蔽 API。

        返回：
            Response: Flask JSON 响应。
        """
        return jsonify({"success": True, "data": []})

    class ApiModule:
        """测试用 API 模块，避免静态路由测试加载 FAISS、DOCX 等重依赖。

        参数：
            无。

        返回：
            None
        """

    fake_api = ApiModule()
    fake_api.chat_bp = chat_bp
    fake_api.kb_bp = kb_bp
    fake_api.roles_bp = roles_bp
    fake_api.sessions_bp = sessions_bp

    # 静态路由测试只关心 Flask 挂载规则，stub API 可避免 FAISS/DOCX 等运行依赖干扰 RED/GREEN 信号。
    monkeypatch.setitem(__import__("sys").modules, "custom_app.api", fake_api)
    monkeypatch.delitem(__import__("sys").modules, "custom_app.app", raising=False)

    from custom_app.app import create_app

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """返回默认内网免登录配置下的 Flask 测试应用。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        Flask: 已开启 TESTING 的应用实例。
    """
    return _create_test_app(tmp_path, monkeypatch)


@pytest.fixture()
def client(app):
    """返回 Flask 测试客户端。

    参数：
        app: 当前测试用的 Flask 应用。

    返回：
        FlaskClient: 可发起 HTTP 请求的测试客户端。
    """
    return app.test_client()


def test_index_route_serves_chat_shell(client):
    """根路径应返回对话页入口。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert 'data-page="chat"' in response.get_data(as_text=True)
    assert "no-store" in (response.headers.get("Cache-Control") or "")


def test_admin_route_serves_admin_shell(client):
    """管理后台路径应返回独立的 admin.html 入口。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/admin")

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert 'data-page="admin"' in response.get_data(as_text=True)


def test_login_route_serves_login_shell(client):
    """登录路径应返回 Phase 4 预留的登录页壳。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/login")

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert 'data-page="login"' in response.get_data(as_text=True)


def test_frontend_static_route_serves_css(client):
    """前端静态资源路径应能直接返回根级 style.css。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/frontend/style.css")

    assert response.status_code == 200
    assert "text/css" in response.content_type
    assert "--color-primary" in response.get_data(as_text=True)


def test_frontend_static_route_serves_favicon_svg(client):
    """前端 favicon 应可通过 /frontend/favicon.svg 访问。"""
    response = client.get("/frontend/favicon.svg")
    assert response.status_code == 200
    assert "svg" in (response.content_type or "")
    assert "<svg" in response.get_data(as_text=True)


def test_frontend_static_route_serves_vendor_js(client):
    """前端静态资源路径应能直接返回 vendor 依赖。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/frontend/vendor/vue.global.prod.js")

    assert response.status_code == 200
    assert "javascript" in response.content_type
    # 只检查关键许可证/构建标记，避免测试绑定到压缩包内部实现细节。
    assert "Vue" in response.get_data(as_text=True)


def test_frontend_routes_do_not_shadow_kb_api(client):
    """前端通配路由不能遮蔽既有知识库 API。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/api/kb/")

    assert response.status_code == 200
    body = response.get_json()
    assert body["success"] is True
    assert isinstance(body["data"], list)


def test_static_responses_have_basic_security_headers(client):
    """静态页面响应应包含基础安全头，降低内网页面被 MIME 嗅探或嵌套的风险。

    参数：
        client: Flask 测试客户端。

    返回：
        None
    """
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_admin_auth_redirects_to_login_when_token_configured(tmp_path, monkeypatch):
    """配置管理 token 后，未授权访问管理后台应跳转登录页。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()

    response = client.get("/admin")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_auth_allows_matching_header_token(tmp_path, monkeypatch):
    """配置管理 token 后，匹配的 Header token 应允许访问管理后台。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()

    response = client.get("/admin", headers={"X-Admin-Token": "secret-token"})

    assert response.status_code == 200
    assert 'data-page="admin"' in response.get_data(as_text=True)


def test_admin_auth_rejects_wrong_header_token(tmp_path, monkeypatch):
    """配置管理 token 后，错误 Header token 仍应被拒绝。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()

    response = client.get("/admin", headers={"X-Admin-Token": "wrong-token"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_admin_auth_allows_matching_cookie_token(tmp_path, monkeypatch):
    """配置管理 token 后，匹配的 Cookie token 应允许访问管理后台。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()
    client.set_cookie("ultrarag_admin_token", "secret-token")

    response = client.get("/admin/")

    assert response.status_code == 200
    assert 'data-page="admin"' in response.get_data(as_text=True)


def test_admin_auth_protects_kb_api_when_token_configured(tmp_path, monkeypatch):
    """配置管理 token 后，知识库管理 API 应要求授权。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()

    response = client.get("/api/kb/")

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_admin_auth_allows_kb_api_with_matching_token(tmp_path, monkeypatch):
    """配置管理 token 后，知识库管理 API 可通过正确 token 访问。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()

    response = client.get("/api/kb/", headers={"X-Admin-Token": "secret-token"})

    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_admin_auth_rejects_cookie_token_for_kb_api(tmp_path, monkeypatch):
    """管理 API 不接受 Cookie token，避免浏览器自动带 Cookie 造成 CSRF。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()
    client.set_cookie("ultrarag_admin_token", "secret-token")

    response = client.get("/api/kb/")

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_admin_auth_does_not_block_login_or_frontend_assets(tmp_path, monkeypatch):
    """配置管理 token 后，登录页与普通前端资源仍应保持可访问。

    参数：
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的运行时补丁工具。

    返回：
        None
    """
    monkeypatch.setenv("ULTRARAG_ADMIN_TOKEN", "secret-token")
    client = _create_test_app(tmp_path, monkeypatch).test_client()

    login_response = client.get("/login")
    css_response = client.get("/frontend/style.css")

    assert login_response.status_code == 200
    assert css_response.status_code == 200
