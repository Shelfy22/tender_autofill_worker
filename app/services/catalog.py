from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from app.config import Settings
from app.models import ProductMatch, ProductMatchItem, TenderPosition
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
        prompt = f"""
Сопоставь позицию тендера с реальным товаром каталога. Используй только candidates.
Полное соответствие — все существенные характеристики соблюдены.
Аналог — допустимая замена. Если подтверждения нет, верни «Товар не найден».
Catalog evidence обязательно должен содержать артикул или ссылку.
Медианная цена — unit price в RUB; quantity здесь не умножай.
Позиция: {json.dumps(product.model_dump(), ensure_ascii=False)}
Candidates: {json.dumps(candidates, ensure_ascii=False, default=str)[:150000]}
""".strip()
        return self.llm.json_call(
            system="Выбери товар из каталога. Только JSON; не выдумывай catalog fields.",
            prompt=prompt,
            schema=ProductMatch,
            operation="catalog_product_selection",
            audit_details=self._position_context,
        )
