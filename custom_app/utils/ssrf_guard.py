"""Phase 7: SSRF 校验（用于 admin 设置 base_url 时）。

策略（与项目实际部署一致）：
    - 只允许 http / https scheme
    - 拒绝几个明显恶意 host：metadata.google.internal、169.254.169.254（云元数据）、
      AWS ec2 metadata 等
    - **默认允许私网**（10/8、192.168/16、172.16/12）——本项目实际有 vLLM 部署在
      192.168.x.x，禁用会破坏现状。如需更严格可设
      ULTRARAG_BLOCK_PRIVATE_BASE_URL=1。

不做：DNS rebinding 防护（DNS 解析后再校验 IP），收益低复杂度高，MVP 跳过。
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse


# 明确禁止：云元数据 IP / 主机名（即便用户配置允许私网也禁）
_HARD_DENY_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata",
    "169.254.169.254",  # AWS / Azure / GCP metadata
    "fd00:ec2::254",    # AWS IMDS IPv6
})


class SSRFRejected(ValueError):
    """base_url 被 SSRF 校验拒绝。"""


def _is_private(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def validate_url_for_ssrf(url: str) -> None:
    """校验通过返回 None；不通过抛 SSRFRejected。

    空字符串视为"用默认 base_url"，直接通过。
    """
    if not url or not url.strip():
        return
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFRejected(
            f"only http/https schemes allowed, got: {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise SSRFRejected("missing hostname")

    host = parsed.hostname.lower()
    if host in _HARD_DENY_HOSTS:
        raise SSRFRejected(f"hostname not allowed: {host}")

    # 默认允许私网；如需严格可通过 env 关闭
    if os.environ.get("ULTRARAG_BLOCK_PRIVATE_BASE_URL", "").strip() == "1":
        if _is_private(host):
            raise SSRFRejected(
                f"private/loopback hostname not allowed under strict mode: {host}"
            )
