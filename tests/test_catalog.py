from __future__ import annotations

import json

import httpx

from app.config import Settings
from app.models import CatalogSelection, ProductMatch, TenderPosition
from app.services.catalog import (
    CatalogMatcher,
    hydrate_catalog_selection,
    limit_catalog_positions,
    normalize_qdrant_candidates,
)


class DummyLlm:
    pass


class SelectionLlm:
    def __init__(self, point_id: str) -> None:
        self.point_id = point_id
        self.prompt = ""
        self.model_chain: list[str] | None = None

    def json_call(self, **values: object) -> CatalogSelection:
        self.prompt = str(values["prompt"])
        self.model_chain = values.get("model_chain")  # type: ignore[assignment]
        assert values["schema"] is CatalogSelection
        return CatalogSelection(
            selectedPointId=self.point_id,
            correspondence="Полное соответствие",
            rationale="Совпадают размеры и количество полок",
        )


class DummyObserver:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def event(self, **values: object) -> None:
        self.events.append(values)


def test_large_tender_catalog_scope_is_limited_to_first_130_positions() -> None:
    positions = [
        TenderPosition(product=f"PRODUCT-{index}")
        for index in range(1, 1001)
    ]

    selected, warnings = limit_catalog_positions(positions, 130)

    assert len(selected) == 130
    assert selected[0].product == "PRODUCT-1"
    assert selected[-1].product == "PRODUCT-130"
    assert positions[130].product == "PRODUCT-131"
    assert any("130" in warning and "1000" in warning for warning in warnings)


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


def test_qdrant_merges_candidates_from_multiple_query_variants() -> None:
    matcher = make_matcher(httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    captured: list[dict[str, object]] = []

    matcher._embedding = lambda query: [1.0 if query == "Светильник" else 2.0]  # type: ignore[method-assign]
    matcher._query_qdrant = lambda vector: (  # type: ignore[method-assign]
        [
            {"id": "shared", "score": 0.70, "payload": {"name": "Общий"}},
            {"id": "base-only", "score": 0.65, "payload": {"name": "Базовый"}},
        ]
        if vector == [1.0]
        else [
            {"id": "shared", "score": 0.95, "payload": {"name": "Точный"}},
            {"id": "exact-only", "score": 0.90, "payload": {"name": "Точный вариант"}},
        ]
    )

    def select(_: TenderPosition, candidates: list[dict[str, object]]) -> ProductMatch:
        captured.extend(candidates)
        return ProductMatch()

    matcher._select_with_llm = select  # type: ignore[method-assign]
    try:
        matcher._qdrant_match(
            TenderPosition(
                product="Светильник",
                searchQueries=["Светильник", "Светильник 40Вт"],
            )
        )
    finally:
        matcher.close()

    assert {item["id"] for item in captured} == {
        "shared",
        "base-only",
        "exact-only",
    }
    shared = next(item for item in captured if item["id"] == "shared")
    assert shared["score"] == 0.95
    assert shared["payload"] == {"name": "Точный"}
    assert shared["matchedQueries"] == ["Светильник", "Светильник 40Вт"]


def test_qdrant_keeps_successful_variant_when_another_variant_fails() -> None:
    matcher = make_matcher(httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    captured: list[dict[str, object]] = []

    matcher._embedding = lambda query: [1.0 if query == "Насос" else 2.0]  # type: ignore[method-assign]

    def query(vector: list[float]) -> list[dict[str, object]]:
        if vector == [2.0]:
            raise TimeoutError("second variant timed out")
        return [{"id": "base", "score": 0.80, "payload": {"name": "Насос"}}]

    matcher._query_qdrant = query  # type: ignore[method-assign]

    def select(_: TenderPosition, candidates: list[dict[str, object]]) -> ProductMatch:
        captured.extend(candidates)
        return ProductMatch()

    matcher._select_with_llm = select  # type: ignore[method-assign]
    try:
        matcher._qdrant_match(
            TenderPosition(
                product="Насос",
                searchQueries=["Насос", "Насос 20м3/ч"],
            )
        )
    finally:
        matcher.close()

    assert [item["id"] for item in captured] == ["base"]


def qdrant_text_candidate(
    point_id: int,
    *,
    name: str,
    price: str | None,
    product_id: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "id": product_id or str(point_id),
        "name": name,
        "vendor": "ГТС",
        "available": "true",
        "url": f"https://www.etm.ru/cat/nn/{product_id or point_id}",
        "currencyId": "RUR",
        "vendorCode": "99-TEST",
    }
    if price is not None:
        metadata["price"] = price
    return {
        "type": "text",
        "text": json.dumps(
            {
                "pageContent": name,
                "metadata": metadata,
                "id": point_id,
            },
            ensure_ascii=False,
        ),
    }


def test_normalize_qdrant_text_payload_preserves_all_prices_and_ids() -> None:
    normalized = normalize_qdrant_candidates(
        [
            qdrant_text_candidate(476338, name="Стеллаж Универсал 2500", price="15707.43"),
            qdrant_text_candidate(2193251, name="Стеллаж Профи 2500", price="17191.56"),
            qdrant_text_candidate(5592435, name="Стеллаж Универсал 2200", price="12265.46"),
        ]
    )

    assert [candidate["pointId"] for candidate in normalized] == [
        "476338",
        "2193251",
        "5592435",
    ]
    assert [candidate["unitPriceRub"] for candidate in normalized] == [
        15707.43,
        17191.56,
        12265.46,
    ]
    assert all(candidate["priceSourceField"] == "payload.metadata.price" for candidate in normalized)
    assert all(candidate["currency"] == "RUB" for candidate in normalized)


def test_selection_uses_only_selected_product_price_not_all_candidate_prices() -> None:
    normalized = normalize_qdrant_candidates(
        [
            qdrant_text_candidate(476338, name="Стеллаж Универсал 2500", price="15707.43"),
            qdrant_text_candidate(2193251, name="Стеллаж Профи 2500", price="17191.56"),
            qdrant_text_candidate(5592435, name="Стеллаж Универсал 2200", price="12265.46"),
        ]
    )
    match = hydrate_catalog_selection(
        CatalogSelection(
            selectedPointId="2193251",
            correspondence="Полное соответствие",
            rationale="Выбран стеллаж требуемой комплектации",
        ),
        normalized,
    )

    assert match.qdrant_point_id == "2193251"
    assert match.article == "2193251"
    assert match.median_price == 17191.56
    assert match.price_source_field == "payload.metadata.price"
    assert match.price_aggregation == "selected_candidate"


def test_duplicate_same_product_prices_use_median_and_missing_selected_price_can_recover() -> None:
    normalized = normalize_qdrant_candidates(
        [
            qdrant_text_candidate(101, name="Один товар", price=None, product_id="ETM-1"),
            qdrant_text_candidate(102, name="Один товар", price="100", product_id="ETM-1"),
            qdrant_text_candidate(103, name="Один товар", price="300", product_id="ETM-1"),
            qdrant_text_candidate(104, name="Другой товар", price="999999", product_id="ETM-2"),
        ]
    )
    match = hydrate_catalog_selection(
        CatalogSelection(
            selectedPointId="101",
            correspondence="Полное соответствие",
            rationale="Выбран ETM-1",
        ),
        normalized,
    )

    assert match.median_price == 200
    assert match.price_aggregation == "median_same_product_id"
    assert "payload.metadata.price" in match.price_source_field


def test_catalog_llm_returns_only_point_id_and_python_hydrates_catalog_fields() -> None:
    llm = SelectionLlm("476338")
    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        catalog_mode="qdrant",
    )
    matcher = CatalogMatcher(settings, llm)  # type: ignore[arg-type]
    try:
        match = matcher._select_with_llm(
            TenderPosition(product="Стеллаж 2500x1060x600 мм", quantity=2),
            [qdrant_text_candidate(476338, name="Стеллаж Универсал 2500", price="15707.43")],
        )
    finally:
        matcher.close()

    assert "не по цене" in llm.prompt
    assert llm.model_chain == settings.models_for_catalog_selection()
    assert match.name == "Стеллаж Универсал 2500"
    assert match.link == "https://www.etm.ru/cat/nn/476338"
    assert match.median_price == 15707.43


def test_catalog_rejects_cable_rack_selected_for_high_voltage_insulator() -> None:
    llm = SelectionLlm("9575060")
    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        catalog_mode="qdrant",
    )
    matcher = CatalogMatcher(settings, llm)  # type: ignore[arg-type]
    try:
        match = matcher._select_with_llm(
            TenderPosition(
                product="С8-1800-II УХЛ1",
                productQuery="С8-1800-II УХЛ1",
                evidence="Категория: ОСИ. Класс напряжения 500 кВ.",
            ),
            [
                qdrant_text_candidate(
                    9575060,
                    name="Стойка кабельная С1800 УХЛ1",
                    price="39252.77",
                )
            ],
        )
    finally:
        matcher.close()

    assert match.correspondence == "Товар не найден"
    assert match.qdrant_point_id is None
    assert "кабельная стойка" in match.rationale
