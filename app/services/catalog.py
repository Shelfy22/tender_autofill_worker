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


def limit_catalog_positions(
    positions: list[TenderPosition], maximum: int
) -> tuple[list[TenderPosition], list[str]]:
    limit = max(1, int(maximum))
    if len(positions) <= limit:
        return positions, []
    warning = (
        f"Большой тендер: поиск в каталоге/Qdrant ограничен первыми {limit} "
        f"позициями из {len(positions)}; coverage рассчитан только по этой выборке."
    )
    return positions[:limit], [warning]


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
                "params": _json_object(params_value) or {},
                "score": raw.get("score"),
                "matchedQueries": raw.get("matchedQueries")
                if isinstance(raw.get("matchedQueries"), list)
                else [],
                "bestQueryIndex": raw.get("bestQueryIndex"),
            }
        )
    return normalized



_NON_TECHNICAL_CHARACTERISTIC = re.compile(
    r"^(?:колонка\s+[a-zа-я]{1,3}\b|цена\b|стоимость\b|"
    r"срок\s+(?:поставки|службы)|количество\s+закупаем|"
    r"однородность\s+совокупности)|н\s*\(?м\)?\s*ц|коэффициент\s+вариации",
    re.IGNORECASE,
)


def _catalog_identity(value: Any) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def _selection_specs(product: TenderPosition) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for characteristic in product.characteristics:
        name = characteristic.name.strip()
        value = characteristic.value.strip()
        if (
            not name or not value
            or characteristic.associationStatus == "conflicting"
            or 0 < characteristic.associationConfidence < 0.80
            or _NON_TECHNICAL_CHARACTERISTIC.search(name)
        ):
            continue
        specs.append((name[:120], value[:180]))
    return list(dict.fromkeys(specs))[:40]


def _catalog_value_matches(requested: str, actual: Any) -> bool:
    expected = _catalog_identity(requested)
    found = _catalog_identity(actual)
    if not expected or not found:
        return False
    if expected == found:
        return True
    expected_numbers = re.findall(r"\d+(?:[.,]\d+)?", requested)
    found_numbers = re.findall(r"\d+(?:[.,]\d+)?", str(actual))
    if (
        len(expected_numbers) != 1 or len(found_numbers) != 1
        or expected_numbers[0].replace(",", ".") != found_numbers[0].replace(",", ".")
    ):
        return False
    requested_words = set(re.findall(r"[a-zа-я]+", expected))
    actual_words = set(re.findall(r"[a-zа-я]+", found))
    return not actual_words or actual_words.issubset(requested_words)


def _selection_affinity(
    product: TenderPosition,
    candidate: dict[str, Any],
    specs: list[tuple[str, str]],
) -> int:
    candidate_identity = _catalog_identity(
        f"{candidate.get('name', '')} {candidate.get('vendorCode', '')}"
    )
    score = sum(
        4
        for identity in (product.model, product.article)
        if len(_catalog_identity(identity)) >= 4
        and _catalog_identity(identity) in candidate_identity
    )
    params = candidate.get("params")
    if not isinstance(params, dict):
        return score
    for name, value in specs:
        name_terms = {word for word in _catalog_identity(name).split() if len(word) >= 3}
        for key, actual in params.items():
            key_terms = {word for word in _catalog_identity(key).split() if len(word) >= 3}
            if name_terms & key_terms and _catalog_value_matches(value, actual):
                score += 2
                break
    return score


def _selection_shortlist(
    product: TenderPosition, candidates: list[dict[str, Any]], limit: int = 10
) -> list[dict[str, Any]]:
    if len(candidates) <= limit:
        return candidates
    head_count = min(7, limit)
    head = candidates[:head_count]
    specs = _selection_specs(product)
    ranked = sorted(
        (
            (_selection_affinity(product, candidate, specs), index, candidate)
            for index, candidate in enumerate(candidates[head_count:])
        ),
        key=lambda item: (-item[0], item[1]),
    )
    promoted = [candidate for score, _, candidate in ranked if score > 0][:limit - len(head)]
    selected_ids = {candidate["pointId"] for candidate in [*head, *promoted]}
    filler = [
        candidate for candidate in candidates[head_count:]
        if candidate["pointId"] not in selected_ids
    ][:limit - len(head) - len(promoted)]
    return [*head, *promoted, *filler]


def _selection_candidates_json(
    candidates: list[dict[str, Any]], specs: list[tuple[str, str]]
) -> str:
    requested_terms = {
        word for name, _ in specs
        for word in _catalog_identity(name).split()
        if len(word) >= 3
    }
    compact: list[dict[str, Any]] = []
    for candidate in candidates:
        params = candidate.get("params") or {}
        ordered_params = (
            sorted(
                params.items(),
                key=lambda pair: -len(
                    {word for word in _catalog_identity(pair[0]).split() if len(word) >= 3}
                    & requested_terms
                ),
            )
            if isinstance(params, dict) else []
        )
        compact.append({
            "pointId": candidate["pointId"],
            "name": str(candidate.get("name") or "")[:300],
            "manufacturer": str(candidate.get("manufacturer") or "")[:100],
            "vendorCode": str(candidate.get("vendorCode") or "")[:100],
            "params": {
                str(key)[:120]: str(value)[:180]
                for key, value in ordered_params[:30]
            },
            "score": candidate.get("score"),
        })
    return json.dumps(compact, ensure_ascii=False, default=str)



_INSULATOR_REQUEST_PATTERN = re.compile(
    r"\b(?:изолятор[а-яё]*|опорн[а-яё]*\s+изолятор[а-яё]*|"
    r"проходн[а-яё]*\s+изолятор[а-яё]*|оси\b)\b",
    re.IGNORECASE,
)
_CABLE_RACK_CANDIDATE_PATTERN = re.compile(
    r"\b(?:стойк[а-яё]*\s+кабельн[а-яё]*|кабельн[а-яё]*\s+стойк[а-яё]*)\b",
    re.IGNORECASE,
)


def _catalog_category_conflict(
    product: TenderPosition,
    selected: dict[str, Any],
) -> str | None:
    """Reject a small set of certain cross-category false positives."""
    requested = " ".join(
        str(value or "")
        for value in (
            product.product,
            product.productQuery,
            product.requirements,
            product.evidence,
        )
    )
    candidate = " ".join(
        str(value or "")
        for value in (
            selected.get("name"),
            selected.get("manufacturer"),
            selected.get("vendorCode"),
            json.dumps(selected.get("params") or {}, ensure_ascii=False, default=str),
        )
    )
    if (
        _INSULATOR_REQUEST_PATTERN.search(requested)
        and _CABLE_RACK_CANDIDATE_PATTERN.search(candidate)
    ):
        return "Запрошен высоковольтный изолятор, но выбран кандидат категории «кабельная стойка»."
    return None


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
            "catalog_params": selected.get("params") if isinstance(selected.get("params"), dict) else {},
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
                    positionKey=product.positionKey,
                    product=product.product,
                    productQuery=product.productQuery or product.product,
                    brand=product.brand,
                    article=product.article,
                    quantity=product.quantity,
                    unit=product.unit,
                    analogsAllowed=product.analogsAllowed,
                    evidence=product.evidence,
                    requirements=product.requirements,
                    characteristics=product.characteristics,
                    searchCharacteristics=product.searchCharacteristics,
                    searchCategory=product.searchCategory,
                    searchCategoryCode=product.searchCategoryCode,
                    searchQueries=product.searchQueries,
                    characteristicConflicts=product.characteristicConflicts,
                    documentUnitPriceRub=product.documentUnitPriceRub,
                    documentLineTotalRub=product.documentLineTotalRub,
                    documentCurrency=product.documentCurrency,
                    documentPriceEvidence=product.documentPriceEvidence,
                    documentPriceSource=product.documentPriceSource,
                    sourceReference=product.sourceReference,
                    sourceCells=product.sourceCells,
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
        canonical_query = (product.productQuery or product.product).strip()
        raw_queries = [canonical_query, *(product.searchQueries or [])]
        base_query = product.product.strip().casefold()
        has_enriched_query = bool(product.searchCharacteristics) and (
            canonical_query.casefold() != base_query
        )
        queries = list(
            dict.fromkeys(
                query.strip()
                for query in raw_queries
                if query and query.strip()
                and not (
                    has_enriched_query
                    and query.strip().casefold() == base_query
                )
            )
        )[: self.settings.qdrant_query_variants]
        merged: dict[str, dict[str, Any]] = {}
        successful_query_count = 0
        last_query_error: Exception | None = None
        for query_index, query in enumerate(queries, start=1):
            self._position_context.update(
                {
                    "searchQuery": query[:300],
                    "searchQueryIndex": query_index,
                    "searchQueryCount": len(queries),
                }
            )
            try:
                vector = self._embedding(query)
                query_candidates = self._query_qdrant(vector)
                successful_query_count += 1
            except Exception as exc:
                last_query_error = exc
                continue
            for candidate in query_candidates:
                point_id = str(candidate.get("id") or "")
                if not point_id:
                    continue
                candidate = {
                    **candidate,
                    "matchedQueries": [query],
                    "bestQueryIndex": query_index,
                }
                existing = merged.get(point_id)
                if existing is None:
                    merged[point_id] = candidate
                    continue
                existing["matchedQueries"] = list(
                    dict.fromkeys([*existing.get("matchedQueries", []), query])
                )
                existing_score = existing.get("score")
                candidate_score = candidate.get("score")
                if (
                    candidate_score is not None
                    and (existing_score is None or float(candidate_score) > float(existing_score))
                ):
                    existing.update(
                        {
                            "score": candidate_score,
                            "payload": candidate.get("payload") or {},
                            "bestQueryIndex": query_index,
                        }
                    )
        if successful_query_count == 0 and last_query_error is not None:
            raise last_query_error
        candidates = sorted(
            merged.values(),
            key=lambda item: (
                item.get("score") is not None,
                float(item.get("score") or 0),
            ),
            reverse=True,
        )[: self.settings.qdrant_merged_candidate_limit]
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
        shortlisted = _selection_shortlist(product, normalized_candidates)
        specs = _selection_specs(product)
        request = {
            "originalProduct": product.product,
            "searchQuery": product.productQuery or product.product,
            "brand": product.brand,
            "model": product.model,
            "article": product.article,
            "analogsAllowed": product.analogsAllowed,
            "requirements": product.requirements[:1500],
            "characteristics": [
                {"name": name, "value": value} for name, value in specs
            ],
        }
        candidates_json = _selection_candidates_json(shortlisted, specs)
        prompt = f"""
Сопоставь исходную позицию тендера с normalizedCandidates. searchQuery использовался
для поиска; окончательный выбор делай по originalProduct, характеристикам и требованиям.
params кандидата и явно указанные параметры в его названии/модели — доказательства.
Отсутствие параметра в params означает «неизвестно», а не «совпадает» или «не совпадает».
Сопоставляй смысл параметров и единицы измерения, а не только одинаковые слова или цифры.
Проверяй ограничения «не менее», «не более», диапазоны и допуски по фактическому значению
кандидата. Не принимай цену, срок поставки, Qdrant score за техническое соответствие.

«Полное соответствие»: совпадает тип/назначение товара и подтверждены все существенные
технические требования; нет известных противоречий. Точная модель или артикул может
подтвердить параметры, явно закодированные в обозначении. При неизвестном существенном
параметре не утверждай полное соответствие.
«Аналог»: тот же тип и назначение, критичные требования совместимы, но исполнение
отличается или часть второстепенных параметров не подтверждена. Выбирай наиболее близкий
по подтверждённым характеристикам, даже если он не первый по Qdrant score.
Если analogsAllowed=false, аналог выбирать нельзя.
Если ни один кандидат не подтверждает совместимость по типу и ключевым параметрам,
верни «Товар не найден» с selectedPointId=null. Совпадение только числа при другом
классе товара недостаточно.

В rationale кратко укажи совпавшие и спорные ключевые параметры; не придумывай
отсутствующие значения. selectedPointId должен быть pointId из normalizedCandidates.
Верни только selectedPointId, correspondence и rationale. Выбирай не по цене.
Позиция: {json.dumps(request, ensure_ascii=False)}
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
            model_chain=self.settings.models_for_catalog_selection(),
        )
        selected = next(
            (
                candidate
                for candidate in shortlisted
                if str(candidate.get("pointId")) == str(selection.selected_point_id)
            ),
            None,
        )
        if selection.selected_point_id and selected is None:
            return hydrate_catalog_selection(selection, shortlisted)
        if product.analogsAllowed is False and selection.correspondence == "Аналог":
            result = NOT_FOUND.model_copy(deep=True)
            result.rationale = "Аналоги запрещены документацией; точное соответствие не подтверждено."
            return result
        if selected is not None:
            conflict = _catalog_category_conflict(product, selected)
            if conflict:
                result = NOT_FOUND.model_copy(deep=True)
                result.rationale = (
                    f"Выбор Qdrant pointId={selection.selected_point_id} отклонён кодом: "
                    f"{conflict} Исходное обоснование LLM: {selection.rationale}"
                )
                return result
        return hydrate_catalog_selection(selection, normalized_candidates)
