"""Path-scoped Cache-Control middleware for the review endpoints.

Applies ``Cache-Control: no-store`` to every HTTP response emitted
for GET /external-actions/{action_id}/review and
GET /external-actions/review-queue -- including
framework-generated 401/404/405/422 rejections -- without
touching any other route. Matching is syntactic and strict:
exactly /external-actions/{id}/review or
/external-actions/review-queue, each with at most one
trailing slash; UUID validity is irrelevant, so
malformed-UUID 422s are covered too. Request bodies, headers,
statuses, and unrelated response headers pass through untouched,
and non-HTTP scopes (lifespan, websocket) bypass entirely.

Known boundary, stated honestly: responses for *unhandled*
exceptions are emitted by Starlette's ServerErrorMiddleware,
which wraps outside all user middleware, so a 500 from an
unexpected bug cannot be stamped here without reimplementing
framework error handling (rejected as unauditable). The review
endpoints map every known failure to a handled response, all of
which this middleware covers.
"""

from __future__ import annotations


def _is_review_path(path: object) -> bool:
    """Match the exact review path, optionally with one trailing slash."""
    if not isinstance(path, str):
        return False

    segments = path.removesuffix("/").split("/")

    return (
        len(segments) == 4
        and segments[0] == ""
        and segments[1] == "external-actions"
        and bool(segments[2])
        and segments[3] == "review"
    )


def _is_review_queue_path(path: object) -> bool:
    """Match the exact queue path, optionally with one trailing slash."""
    if not isinstance(path, str):
        return False

    segments = path.removesuffix("/").split("/")

    return (
        len(segments) == 3
        and segments[0] == ""
        and segments[1] == "external-actions"
        and segments[2] == "review-queue"
    )


class ReviewNoStoreMiddleware:
    """Stamp no-store on review-endpoint responses (pure ASGI)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (
            _is_review_path(path) or _is_review_queue_path(path)
        ):
            await self.app(scope, receive, send)
            return

        async def _send_with_no_store(message) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if bytes(name).lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send_with_no_store)
