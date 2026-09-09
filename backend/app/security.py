"""Security response headers.

Set here rather than only in Nginx so they hold however the application is
served: behind the production proxy, straight from Uvicorn with
SERVE_STATIC_DIR, or through a tunnel pointed at a development machine. Nginx
sets them too, and a duplicate is harmless - the browser takes the strictest
interpretation of a repeated CSP.

The policy is deliberately tight, and every relaxation below is there for a
reason that was verified rather than assumed.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

# The built page loads one module script and one stylesheet, both from this
# origin, and there is no inline script anywhere - so script-src needs neither
# 'unsafe-inline' nor 'unsafe-eval', which is where most of the value is.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        # Nothing is framed, and nothing may frame us. This is what replaces
        # X-Frame-Options for browsers that support it.
        "frame-ancestors 'none'",
        "frame-src 'none'",
        "object-src 'none'",
        # A dangling <base> would let injected markup retarget every relative
        # URL on the page, including the upload endpoints.
        "base-uri 'none'",
        "form-action 'self'",
        "script-src 'self'",
        # 'unsafe-inline' is required and cannot be avoided here. CodeMirror
        # injects its theme through style-mod, which creates a <style> element
        # at runtime, and y-codemirror.next paints remote selections with a
        # style attribute on each decoration. Both are blocked by a strict
        # style-src. It costs little: styles cannot exfiltrate data the way
        # script can, and script-src stays strict.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        # Same-origin covers the WebSocket too: 'self' matches ws:// and wss://
        # on this host. Verified against a real connection rather than trusted.
        "connect-src 'self'",
        "media-src 'self' blob:",
        "worker-src 'self' blob:",
        "manifest-src 'self'",
    )
)

BASE_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    # Room codes live in the URL, so no referrer should ever leave with them.
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    # Redundant with frame-ancestors, kept for anything that predates CSP3.
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    # None of these are used; denying them shrinks what an injected script
    # could reach for.
    (
        b"permissions-policy",
        b"accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        b"magnetometer=(), microphone=(), payment=(), usb=()",
    ),
)

# Two years, subdomains included, and preload-eligible. Only ever sent over
# HTTPS: a browser ignores it on a plain connection, and sending it there would
# make local development look secure when it is not.
STRICT_TRANSPORT_SECURITY = b"max-age=63072000; includeSubDomains; preload"


class SecurityHeadersMiddleware:
    """Pure ASGI, so it can leave WebSocket handshakes alone.

    Starlette's BaseHTTPMiddleware only sees `http` scopes and would buffer
    responses; the download endpoint streams, so buffering it would defeat the
    point of streaming at all.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        secure = _is_https(scope)

        async def send_with_headers(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in BASE_HEADERS:
                    if name not in present:
                        headers.append((name, value))
                if secure and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", STRICT_TRANSPORT_SECURITY))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _is_https(scope: Scope) -> bool:
    """Whether the browser's connection is HTTPS.

    Uvicorn runs on loopback behind a proxy, so the scheme in the scope is
    `http` even when the user is on HTTPS; the proxy's X-Forwarded-Proto is
    what tells the truth. Uvicorn is started with --proxy-headers and
    --forwarded-allow-ips 127.0.0.1, so that header is only trusted from the
    local proxy.
    """
    if scope.get("scheme") in ("https", "wss"):
        return True
    headers: list[tuple[bytes, bytes]] = list(scope.get("headers", ()))
    for name, value in headers:
        if name == b"x-forwarded-proto":
            # A chain of proxies appends, so the client-facing scheme is first.
            return value.split(b",")[0].strip() == b"https"
    return False
