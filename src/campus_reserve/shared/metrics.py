from time import perf_counter
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_REQUESTS = Counter(
    "campus_reserve_http_requests_total",
    "Total HTTP requests handled by the API.",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "campus_reserve_http_request_duration_seconds",
    "API request duration in seconds.",
    ("method", "route"),
)


class PrometheusMiddleware:
    """Small ASGI metrics layer without coupling to FastAPI router internals."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return

        started_at = perf_counter()
        status_code = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route: Any = scope.get("route")
            route_name = getattr(route, "path", "unmatched")
            method = scope.get("method", "UNKNOWN")
            HTTP_REQUESTS.labels(method, route_name, str(status_code)).inc()
            HTTP_DURATION.labels(method, route_name).observe(perf_counter() - started_at)


async def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
