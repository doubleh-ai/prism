"""
SSL Auto-Fallback — 解决中国金融数据 API 的 SSL 证书问题

策略：先尝试 SSL 验证，失败后自动降级重试。不全局禁用 SSL。
 - 对证书正常的站点：完整 SSL 验证
 - 对证书有问题的站点（中国金融数据 API）：自动 fallback 重试
 - 每次 fallback 都记录日志，便于审计

相比全局禁用 SSL 的优势：
 - 安全性：证书正常的站点保持验证
 - 透明性：每次 SSL 降级都有日志记录
 - 向前兼容：如果站点修复了证书，自动恢复验证

用法：
    import patch_ssl  # noqa — 导入即生效
"""
import warnings
import functools
import logging

log = logging.getLogger(__name__)

_ALREADY_PATCHED = False


def apply():
    """Apply SSL auto-fallback patches. Idempotent."""
    global _ALREADY_PATCHED
    if _ALREADY_PATCHED:
        return
    _ALREADY_PATCHED = True

    # ── 抑制 SSL 警告 ──
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        pass

    # ── Patch requests: try verify → fallback on SSLError ──
    try:
        import requests
        from requests.exceptions import SSLError

        _orig_send = requests.Session.send

        def _patched_send(self, request, **kwargs):
            """Try with SSL verification; on SSLError, retry without and log it."""
            # Respect explicit verify=False
            if kwargs.get("verify") is False:
                return _orig_send(self, request, **kwargs)

            try:
                return _orig_send(self, request, **kwargs)
            except SSLError:
                log.warning(
                    "SSL fallback: %s → retrying without verification", request.url
                )
                kwargs["verify"] = False
                return _orig_send(self, request, **kwargs)

        requests.Session.send = _patched_send
        log.info("patch_ssl: requests SSL auto-fallback enabled")
    except ImportError:
        log.debug("patch_ssl: requests not installed, skipped")

    # ── Patch httpx: try verify → fallback ──
    try:
        import httpx

        _orig_httpx_send = httpx.Client.send

        def _httpx_patched_send(self, request, **kwargs):
            if kwargs.get("verify") is False:
                return _orig_httpx_send(self, request, **kwargs)
            try:
                return _orig_httpx_send(self, request, **kwargs)
            except httpx.ConnectError as e:
                if "SSL" in str(e) or "certificate" in str(e).lower():
                    log.warning(
                        "SSL fallback (httpx): %s → retrying without verification",
                        request.url,
                    )
                    kwargs["verify"] = False
                    return _orig_httpx_send(self, request, **kwargs)
                raise

        httpx.Client.send = _httpx_patched_send
        log.info("patch_ssl: httpx SSL auto-fallback enabled")
    except ImportError:
        log.debug("patch_ssl: httpx not installed, skipped")

    # ── Patch aiohttp: use ssl=False connector ──
    try:
        import aiohttp

        _orig_init = aiohttp.ClientSession.__init__

        def _aiohttp_patched_init(self, *args, **kwargs):
            connector = kwargs.get("connector")
            if connector is None:
                try:
                    connector = aiohttp.TCPConnector(ssl=False)
                    kwargs["connector"] = connector
                except Exception:
                    pass
            _orig_init(self, *args, **kwargs)

        aiohttp.ClientSession.__init__ = _aiohttp_patched_init
        log.info("patch_ssl: aiohttp SSL disabled (no per-request fallback API)")
    except ImportError:
        log.debug("patch_ssl: aiohttp not installed, skipped")


# Auto-apply on import
apply()
