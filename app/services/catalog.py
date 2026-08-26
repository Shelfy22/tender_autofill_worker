from __future__ import annotations

import json
import re
import statistics
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import CatalogSelection, ProductMatch, ProductMatchItem, TenderPosition
from app.services.llm import LlmClient

if TYPE_CHECKING:
    from app.observability import RunObserver


NOT_FOUND = ProductMatch(
    **{
        "Артикул": None,
        "Ссылка": None,
        "Наименование": "Товар не найден",
        "Производитель": "Товар не найден",
        "Медианная цена": None,
        "Валюта": None,
        "Источник цены": "",
        "Обоснование": "Товар не найден",
        "Соответствие": "Товар не найден",
    }
)


PRICE_FIELD_NAMES = (
    "price",
    "Медианная цена",
    "Медианная цена, руб.",
    "Цена",
    "medianPrice",
    "median_price",
)


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_value(
    sources: list[tuple[str, dict[str, Any]]],
    keys: tuple[str, ...],
) -> tuple[Any, str]:
    for prefix, source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and str(value).strip() != "":
                return value, f"{prefix}.{key}" if prefix else key
    return None, ""


def _normalize_currency(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    return "RUB" if text in {"RUR", "РУБ", "РУБ.", "₽"} else text


def _normalize_available(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "да"}:
        return True
    if text in {"false", "0", "no", "нет"}:
        return False
    return None


def _catalog_id_from_url(value: Any) -> str | None:
    match = re.search(r"/cat/nn/([^/?#]+)", str(value or ""), re.I)
    return match.group(1).strip() if match else None


def normalize_qdrant_candidates(candidates: Any) -> list[dict[str, Any]]:
    """Convert Qdrant/n8n document payloads into a stable, compact selection contract."""
    raw_items = candidates
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("candidates", raw_items.get("points", raw_items.get("result")))
    if not isinstance(raw_items, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen_point_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        if not isinstance(payload, dict):
            continue
        document = _json_object(payload.get("text")) or payload
        nested_payload = (
            document.get("payload") if isinstance(document.get("payload"), dict) else {}
        )
        metadata = document.get("metadata")
        if isinstance(metadata, str):
            metadata = _json_object(metadata)
        if not isinstance(metadata, dict):
            metadata = {}

        sources = [
            ("payload.metadata", metadata),
            ("payload", document),
            ("payload.payload", nested_payload),
            ("point", raw),
        ]
        product_id_value, _ = _first_value(
            sources,
            ("productId", "product_id", "id", "article", "Артикул"),
        )
        url_value, _ = _first_value(sources, ("url", "link", "Ссылка"))
        product_id = str(product_id_value).strip() if product_id_value is not None else ""
        if not product_id:
            product_id = _catalog_id_from_url(url_value) or ""

        raw_point_id = raw.get("id") if raw.get("payload") is not None else None
        point_id_value = raw_point_id if raw_point_id is not None else document.get("id")
        point_id = str(point_id_value if point_id_value is not None else product_id).strip()
        if not point_id or point_id in seen_point_ids:
            continue
        seen_point_ids.add(point_id)

        price_value, price_source_field = _first_value(sources, PRICE_FIELD_NAMES)
        unit_price = ProductMatch.model_validate(
            {"Медианная цена": price_value}
        ).median_price
        currency_value, _ = _first_value(
            sources,
            ("currencyId", "currency", "currency_id", "Валюта"),
        )
        name_value, _ = _first_value(
            sources,
            ("name", "Наименование", "pageContent", "content"),
        )
        manufacturer_value, _ = _first_value(
            sources,
            ("vendor", "manufacturer", "Производитель", "brand"),
        )
        vendor_code_value, _ = _first_value(
            sources,
            ("vendorCode", "vendor_code", "Код производителя"),
        )
        available_value, _ = _first_value(sources, ("available", "inStock", "in_stock"))
        params_value, _ = _first_value(sources, ("params", "parameters", "Характеристики"))

        normalized.append(
            {
                "pointId": point_id,
                "productId": product_id or point_id,
                "name": str(name_value or "").strip(),
                "manufacturer": str(manufacturer_value or "").strip(),
                "vendorCode": str(vendor_code_value or "").strip(),
                "url": str(url_value or "").strip(),
                "unitPriceRub": unit_price,
                "currency": _normalize_currency(currency_value)
                or ("RUB" if unit_price is not None else None),
                "priceSourceField": price_source_field if unit_price is not None else "",
                "available": _normalize_available(available_value),
                "params": params_value if isinstance(params_value, dict) else {},
                "score": raw.get("score"),
            }
        )
    return normalized


def _selection_candidates_json(candidates: list[dict[str, Any]], limit: int = 150_000) -> str:
    serialized = json.dumps(candidates, ensure_ascii=False, default=str)
    if len(serialized) <= limit:
        return serialized

    trimmed: list[dict[str, Any]] = []
    for candidate in candidates:
        compact = dict(candidate)
        params = compact.get("params")
        if isinstance(params, dict):
            compact["params"] = {
                str(key)[:200]: str(value)[:500]
                for key, value in list(params.items())[:30]
            }
        trimmed.append(compact)
    serialized = json.dumps(trimmed, ensure_ascii=False, default=str)
    if len(serialized) <= limit:
        return serialized

    for candidate in trimmed:
        candidate["params"] = {}
    return json.dumps(trimmed, ensure_ascii=False, default=str)


def hydrate_catalog_selection(
    selection: CatalogSelection,
    candidates: list[dict[str, Any]],
) -> ProductMatch:
    if selection.correspondence == "Товар не найден" or not selection.selected_point_id:
        result = NOT_FOUND.model_copy(deep=True)
        result.rationale = selection.rationale or result.rationale
        return result

    selected = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("pointId")) == selection.selected_point_id
        ),
        None,
    )
    if selected is None:
        result = NOT_FOUND.model_copy(deep=True)
        result.rationale = (
            f"LLM вернул неизвестный selectedPointId={selection.selected_point_id}. "
            f"{selection.rationale}"
        ).strip()
        return result

    product_id = str(selected.get("productId") or "").strip()
    same_product = [
        candidate
        for candidate in candidates
        if product_id and str(candidate.get("productId") or "").strip() == product_id
    ] or [selected]
    priced_same_product = [
        candidate for candidate in same_product if candidate.get("unitPriceRub") is not None
    ]
    prices = [float(candidate["unitPriceRub"]) for candidate in priced_same_product]
    unit_price = float(statistics.median(prices)) if prices else None

    if len(priced_same_product) > 1:
        price_aggregation = "median_same_product_id"
        source_fields = sorted(
            {
                str(candidate.get("priceSourceField") or "")
                for candidate in priced_same_product
                if candidate.get("priceSourceField")
            }
        )
        price_source_field = "median(" + ", ".join(source_fields) + ")"
    elif priced_same_product:
        source_candidate = priced_same_product[0]
        price_aggregation = (
            "selected_candidate"
            if source_candidate is selected
            else "same_product_id_fallback"
        )
        price_source_field = str(source_candidate.get("priceSourceField") or "")
    else:
        price_aggregation = "unavailable"
        price_source_field = ""

    price_candidate = priced_same_product[0] if priced_same_product else selected
    article = product_id or _catalog_id_from_url(selected.get("url"))
    link = str(selected.get("url") or "").strip()
    if not link and article:
        link = f"https://www.etm.ru/cat/nn/{article}"
    currency = _normalize_currency(price_candidate.get("currency")) if unit_price is not None else None

    return ProductMatch.model_validate(
        {
            "Артикул": article,
            "Ссылка": link or None,
            "Наименование": selected.get("name") or None,
            "Производитель": selected.get("manufacturer") or None,
            "Медианная цена": unit_price,
            "Валюта": currency or ("RUB" if unit_price is not None else None),
            "Источник цены": (
                f"Qdrant: {price_source_field}" if price_source_field else ""
            ),
            "Поле цены": price_source_field,
            "Метод цены": price_aggregation,
            "Qdrant point ID": selected.get("pointId"),
            "ID товара": product_id or article,
            "Обоснование": selection.rationale,
            "Соответствие": selection.correspondence,
        }
    )


class CatalogMatcher:
    def __init__(
        self,
        settings: Settings,
        llm: LlmClient,
        observer: "RunObserver | None" = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.observer = observer
        self._position_context: dict[str, Any] = {}
        self.http = httpx.Client(timeout=settings.catalog_timeout_seconds)

    def close(self) -> None:
        self.http.close()

    def match_all(self, products: list[TenderPosition]) -> tuple[list[ProductMatchItem], list[str]]:
        warnings: list[str] = []
        result: list[ProductMatchItem] = []
        for index, product in enumerate(products, start=1):
            self._position_context = {
                "positionIndex": index,
                "productQuery": (product.productQuery or product.product)[:300],
            }
            try:
                match = self._match(product)
            except Exception as exc:
                match = NOT_FOUND.model_copy(deep=True)
                match.rationale = f"Ошибка поиска: {exc}"
                warnings.append(f"{product.product}: catalog search error: {exc}")
            result.append(
                ProductMatchItem(
                    positionIndex=index,
                    product=product.product,
                    productQuery=product.productQuery or product.product,
                    brand=product.brand,
                    article=product.article,
                    quantity=product.quantity,
                    unit=product.unit,
                    analogsAllowed=product.analogsAllowed,
                    evidence=product.evidence,
                    requirements=product.requirements,
                    documentUnitPriceRub=product.documentUnitPriceRub,
                    documentLineTotalRub=product.documentLineTotalRub,
                    documentCurrency=product.documentCurrency,
                    documentPriceEvidence=product.documentPriceEvidence,
                    documentPriceSource=product.documentPriceSource,
                    match=match,
                )
            )
        self._position_context = {}
        if self.settings.catalog_mode == "disabled" and products:
            warnings.append(
                "Catalog matching отключён: настройте CATALOG_MODE=http или qdrant. "
                "Exported n8n workflow не содержит соединённого product-match контракта."
            )
        return result, warnings

    def _match(self, product: TenderPosition) -> ProductMatch:
        if self.settings.catalog_mode == "disabled":
            return NOT_FOUND.model_copy(deep=True)
        if self.settings.catalog_mode == "http":
            return self._http_match(product)
        return self._qdrant_match(product)

    def _http_match(self, product: TenderPosition) -> ProductMatch:
        if not self.settings.catalog_search_url:
            raise RuntimeError("CATALOG_SEARCH_URL не настроен")
        headers = {"Accept": "application/json"}
        if self.settings.catalog_api_key:
            headers["Authorization"] = f"Bearer {self.settings.catalog_api_key.get_secret_value()}"
        response = self.http.post(
            self.settings.catalog_search_url,
            headers=headers,
            json=product.model_dump(),
        )
        response.raise_for_status()
        data = response.json()
        direct = data.get("match") if isinstance(data, dict) else None
        if isinstance(direct, dict):
            return ProductMatch.model_validate(direct)
        candidates = data.get("candidates", data) if isinstance(data, dict) else data
        return self._select_with_llm(product, candidates)

    def _embedding(self, text: str) -> list[float]:
        if not self.settings.ollama_url:
            raise RuntimeError("OLLAMA_URL не настроен")
        base = self.settings.ollama_url.rstrip("/")
        logical_started = time.monotonic()
        error: Exception | None = None
        vector: list[float] = []
        try:
            response = self._catalog_http_post(
                service="ollama_http",
                operation="embed",
                url=f"{base}/api/embed",
                json_body={"model": self.settings.ollama_embedding_model, "input": text},
                counters={"embedding_http_requests": 1},
            )
            if response.status_code == 404:
                response = self._catalog_http_post(
                    service="ollama_http",
                    operation="embeddings_legacy",
                    url=f"{base}/api/embeddings",
                    json_body={"model": self.settings.ollama_embedding_model, "prompt": text},
                    counters={"embedding_http_requests": 1},
                )
            response.raise_for_status()
            data = response.json()
            if isinstance(data.get("embeddings"), list) and data["embeddings"]:
                vector = [float(value) for value in data["embeddings"][0]]
            elif isinstance(data.get("embedding"), list):
                vector = [float(value) for value in data["embedding"]]
            else:
                raise RuntimeError("Ollama не вернул embedding")
            return vector
        except Exception as exc:
            error = exc
            raise
        finally:
            if self.observer:
                self.observer.event(
                    event_type="external_call",
                    status="completed" if error is None else "failed",
                    stage="catalog_embedding",
                    service="embedding",
                    operation="embedding_query",
                    model=self.settings.ollama_embedding_model,
                    duration_seconds=round(time.monotonic() - logical_started, 3),
                    result_count=len(vector),
                    error=error,
                    details={"vectorDimensions": len(vector), **self._position_context},
                    counters={"embedding_queries": 1},
                )

    def _qdrant_match(self, product: TenderPosition) -> ProductMatch:
        if not self.settings.qdrant_url:
            raise RuntimeError("QDRANT_URL не настроен")
        vector = self._embedding(product.productQuery or product.product)
        candidates = self._query_qdrant(vector)
        return self._select_with_llm(product, candidates)

    def _query_qdrant(self, vector: list[float]) -> list[dict[str, Any]]:
        """Query the remote Qdrant REST API without installing its Python SDK."""
        if not self.settings.qdrant_url:
            raise RuntimeError("QDRANT_URL не настроен")

        collection = quote(self.settings.qdrant_collection, safe="")
        base_url = self.settings.qdrant_url.rstrip("/")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.settings.qdrant_api_key:
            headers["api-key"] = self.settings.qdrant_api_key.get_secret_value()

        legacy_vector: list[float] | dict[str, Any] = vector
        if self.settings.qdrant_vector_name:
            legacy_vector = {
                "name": self.settings.qdrant_vector_name,
                "vector": vector,
            }

        query_body: dict[str, Any] = {
            "query": vector,
            "limit": self.settings.qdrant_top_k,
            "with_payload": True,
            "with_vector": False,
        }
        if self.settings.qdrant_vector_name:
            query_body["using"] = self.settings.qdrant_vector_name

        logical_started = time.monotonic()
        error: Exception | None = None
        candidates: list[dict[str, Any]] = []
        try:
            response = self._catalog_http_post(
                service="qdrant_http",
                operation="points_query",
                url=f"{base_url}/collections/{collection}/points/query",
                headers=headers,
                json_body=query_body,
                counters={"qdrant_http_requests": 1},
            )

            # Qdrant before 1.10 uses /points/search. Fall back only when the
            # endpoint itself is unavailable; do not hide authentication/schema errors.
            if response.status_code in {404, 405}:
                response = self._catalog_http_post(
                    service="qdrant_http",
                    operation="points_search_legacy",
                    url=f"{base_url}/collections/{collection}/points/search",
                    headers=headers,
                    json_body={
                        "vector": legacy_vector,
                        "limit": self.settings.qdrant_top_k,
                        "with_payload": True,
                        "with_vector": False,
                    },
                    counters={"qdrant_http_requests": 1},
                )
            response.raise_for_status()

            data = response.json()
            raw_result = data.get("result") if isinstance(data, dict) else None
            raw_points = raw_result.get("points") if isinstance(raw_result, dict) else raw_result
            if not isinstance(raw_points, list):
                raise RuntimeError("Qdrant вернул неожиданный формат ответа")

            for point in raw_points:
                if not isinstance(point, dict):
                    continue
                candidates.append(
                    {
                        "score": point.get("score"),
                        "id": str(point.get("id")),
                        "payload": point.get("payload") or {},
                    }
                )
            return candidates
        except Exception as exc:
            error = exc
            raise
        finally:
            if self.observer:
                self.observer.event(
                    event_type="external_call",
                    status="completed" if error is None else "failed",
                    stage="catalog_qdrant",
                    service="qdrant",
                    operation="logical_query",
                    duration_seconds=round(time.monotonic() - logical_started, 3),
                    result_count=len(candidates),
                    error=error,
                    details={
                        "collection": self.settings.qdrant_collection,
                        "topK": self.settings.qdrant_top_k,
                        **self._position_context,
                    },
                    counters={"qdrant_queries": 1, "qdrant_results": len(candidates)},
                )

    def _catalog_http_post(
        self,
        *,
        service: str,
        operation: str,
        url: str,
        json_body: Any,
        counters: dict[str, int],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        started = time.monotonic()
        response: httpx.Response | None = None
        error: Exception | None = None
        try:
            response = self.http.post(url, headers=headers, json=json_body)
            return response
        except Exception as exc:
            error = exc
            raise
        finally:
            if self.observer:
                self.observer.event(
                    event_type="external_call",
                    status=(
                        "failed"
                        if error is not None or (response is not None and response.status_code >= 400)
                        else "completed"
                    ),
                    stage="catalog_http",
                    service=service,
                    operation=operation,
                    model=(
                        self.settings.ollama_embedding_model
                        if service == "ollama_http"
                        else None
                    ),
                    http_method="POST",
                    http_status=response.status_code if response is not None else None,
                    duration_seconds=round(time.monotonic() - started, 3),
                    error=error,
                    details={
                        "collection": self.settings.qdrant_collection
                        if service == "qdrant_http"
                        else None,
                        **self._position_context,
                    },
                    counters=counters,
                )

    def _select_with_llm(self, product: TenderPosition, candidates: Any) -> ProductMatch:
        normalized_candidates = normalize_qdrant_candidates(candidates)
        if not normalized_candidates:
            result = NOT_FOUND.model_copy(deep=True)
            result.rationale = "Qdrant не вернул нормализуемых кандидатов"
            return result
        candidates_json = _selection_candidates_json(normalized_candidates)
        prompt = f"""
Сопоставь позицию тендера с реальным товаром каталога. Используй только normalizedCandidates.
Полное соответствие — все существенные характеристики соблюдены.
Аналог — допустимая замена. Если подтверждения нет, верни «Товар не найден».
Выбирай по назначению и существенным техническим характеристикам, а не по цене.
selectedPointId обязан точно совпадать с pointId одного кандидата. Не возвращай цену,
артикул, ссылку, название или производителя: Python возьмёт их из выбранного payload.
Если подходящего кандидата нет, верни selectedPointId=null и «Товар не найден».
Позиция: {json.dumps(product.model_dump(), ensure_ascii=False)}
normalizedCandidates: {candidates_json}
""".strip()
        selection = self.llm.json_call(
            system=(
                "Выбери pointId товара из каталога. Верни только selectedPointId, "
                "correspondence и rationale; не копируй catalog fields."
            ),
            prompt=prompt,
            schema=CatalogSelection,
            operation="catalog_product_selection",
            audit_details=self._position_context,
        )
        return hydrate_catalog_selection(selection, normalized_candidates)
