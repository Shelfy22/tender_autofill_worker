from __future__ import annotations

import httpx

from app.config import Settings
from app.services.catalog import CatalogMatcher


class DummyLlm:
    pass


class DummyObserver:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def event(self, **values: object) -> None:
        self.events.append(values)


def make_matcher(
    handler: httpx.MockTransport,
    observer: DummyObserver | None = None,
) -> CatalogMatcher:
    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        catalog_mode="qdrant",
        qdrant_url="https://qdrant.example",
        qdrant_api_key="secret",
        qdrant_collection="products/current",
        qdrant_top_k=5,
    )
    matcher = CatalogMatcher(
        settings,
        DummyLlm(),  # type: ignore[arg-type]
        observer=observer,  # type: ignore[arg-type]
    )
    matcher.http.close()
    matcher.http = httpx.Client(transport=handler)
    return matcher


def test_qdrant_uses_remote_rest_query_api() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/collections/products%2Fcurrent/points/query"
        assert request.headers["api-key"] == "secret"
        body = request.read().decode("utf-8")
        assert '"query":[0.1,0.2]' in body
        assert '"limit":5' in body
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": {
                    "points": [
                        {"id": 42, "score": 0.91, "payload": {"article": "A-42"}}
                    ]
                },
            },
        )

    matcher = make_matcher(httpx.MockTransport(handle))
    try:
        assert matcher._query_qdrant([0.1, 0.2]) == [
            {"id": "42", "score": 0.91, "payload": {"article": "A-42"}}
        ]
    finally:
        matcher.close()


def test_qdrant_falls_back_to_legacy_search_endpoint() -> None:
    paths: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path)
        if request.url.path.endswith("/points/query"):
            return httpx.Response(404, json={"status": "not found"})
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "result": [{"id": "old", "score": 0.75, "payload": {}}],
            },
        )

    matcher = make_matcher(httpx.MockTransport(handle))
    try:
        points = matcher._query_qdrant([0.3, 0.4])
    finally:
        matcher.close()

    assert paths == [
        b"/collections/products%2Fcurrent/points/query",
        b"/collections/products%2Fcurrent/points/search",
    ]
    assert points == [{"id": "old", "score": 0.75, "payload": {}}]


def test_qdrant_observability_separates_logical_query_from_http_requests() -> None:
    observer = DummyObserver()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/points/query"):
            return httpx.Response(404, json={"status": "not found"})
        return httpx.Response(200, json={"result": []})

    matcher = make_matcher(httpx.MockTransport(handle), observer)
    try:
        assert matcher._query_qdrant([0.1, 0.2]) == []
    finally:
        matcher.close()

    http_events = [event for event in observer.events if event["service"] == "qdrant_http"]
    logical_events = [event for event in observer.events if event["service"] == "qdrant"]
    assert len(http_events) == 2
    assert len(logical_events) == 1
    assert logical_events[0]["status"] == "completed"
