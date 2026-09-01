from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from json_repair import repair_json
from openai import OpenAI
from pydantic import BaseModel

from app.config import Settings
from app.models import ExtractedFieldsResponse, LlmDecision, TenderPositionsResponse

if TYPE_CHECKING:
    from app.observability import RunObserver


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class JsonExtractionResult:
    value: dict[str, Any] | list[Any]
    source: str
    repaired: bool = False
    initial_error: json.JSONDecodeError | None = None


class LlmResponseTruncatedError(RuntimeError):
    """The provider stopped before returning a complete structured response."""


PRODUCT_DIRECT_CALL_MAX_CHARS = 30_000
PRODUCT_CHUNK_TARGET_CHARS = 12_000
PRODUCT_CHUNK_MAX_DEPTH = 8


def _split_product_text(
    text: str,
    *,
    target_chars: int = PRODUCT_CHUNK_TARGET_CHARS,
) -> list[str]:
    source = str(text or "").strip()
    if not source:
        return []
    target = max(1_000, int(target_chars))
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in source.splitlines(keepends=True):
        if len(line) > target:
            if current:
                chunks.append("".join(current).strip())
                current = []
                current_length = 0
            chunks.extend(
                line[index:index + target].strip()
                for index in range(0, len(line), target)
                if line[index:index + target].strip()
            )
            continue
        if current and current_length + len(line) > target:
            chunks.append("".join(current).strip())
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line)
    if current:
        chunks.append("".join(current).strip())
    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) >= 2:
        return chunks
    midpoint = max(1, len(source) // 2)
    return [chunk for chunk in (source[:midpoint].strip(), source[midpoint:].strip()) if chunk]


def _json_candidates(source: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = [("raw", source)]
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", source, re.I)
    if fenced:
        candidates.append(("markdown_fence", fenced.group(1).strip()))

    object_start, object_end = source.find("{"), source.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(("object_bounds", source[object_start : object_end + 1]))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, candidate in candidates:
        if candidate and candidate not in seen:
            unique.append((name, candidate))
            seen.add(candidate)
    return unique


def extract_json_result(
    text: str,
    *,
    allow_repair: bool = True,
) -> JsonExtractionResult:
    source = str(text or "").strip()
    if not source:
        raise ValueError("LLM вернул пустой ответ")

    candidates = _json_candidates(source)
    first_error: json.JSONDecodeError | None = None
    last_error: json.JSONDecodeError | None = None
    for name, candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            last_error = exc
            continue
        if isinstance(decoded, (dict, list)):
            return JsonExtractionResult(value=decoded, source=name)

    if not allow_repair:
        if last_error is not None:
            raise last_error
        raise ValueError("LLM не вернул валидный JSON")

    # Prefer a fenced or explicitly bounded payload over surrounding prose when
    # invoking the permissive repair parser. Pydantic validation remains mandatory.
    repair_priority = {
        "markdown_fence": 0,
        "object_bounds": 1,
        "raw": 3,
    }

    def candidate_priority(item: tuple[str, str]) -> int:
        name, candidate = item
        if name == "raw" and candidate.lstrip().startswith("{"):
            return 1
        return repair_priority.get(name, 99)

    repair_candidates = sorted(
        candidates,
        key=candidate_priority,
    )
    for name, candidate in repair_candidates:
        try:
            repaired = repair_json(
                candidate,
                return_objects=True,
                ensure_ascii=False,
                skip_json_loads=True,
            )
        except (ValueError, TypeError, IndexError):
            continue
        if isinstance(repaired, (dict, list)):
            return JsonExtractionResult(
                value=repaired,
                source=name,
                repaired=True,
                initial_error=first_error,
            )

    if last_error is not None:
        raise last_error
    raise ValueError("LLM не вернул валидный JSON")


def extract_json(text: str) -> dict[str, Any] | list[Any]:
    return extract_json_result(text).value


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Pydantic's validation schema to a strict output schema.

    Strict structured-output providers require every declared property and reject
    undeclared properties. Nullable fields remain nullable through their anyOf.
    """
    strict_schema = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        for definition in (node.get("$defs") or {}).values():
            visit(definition)
        for item in node.get("anyOf") or []:
            visit(item)
        if isinstance(node.get("items"), dict):
            visit(node["items"])
        properties = node.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                visit(child)
            node["required"] = list(properties)
            node["additionalProperties"] = False
        elif isinstance(node.get("additionalProperties"), dict):
            visit(node["additionalProperties"])

    visit(strict_schema)
    return strict_schema


def _response_schema(schema: type[T], *, strict: bool) -> dict[str, Any]:
    generated = schema.model_json_schema()
    if not strict:
        return generated

    generated = _strict_json_schema(generated)
    if schema is ExtractedFieldsResponse:
        # Dynamic dictionary keys are incompatible with strict schemas. The worker
        # has a fixed field contract, so expose those keys explicitly to the model.
        field_value = generated.get("$defs", {}).get("FieldValue")
        if isinstance(field_value, dict):
            value_schema = field_value.get("properties", {}).get("value")
            if isinstance(value_schema, dict):
                value_schema.clear()
                value_schema["anyOf"] = [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "boolean"},
                    {"type": "null"},
                ]
        fields_schema = generated.get("properties", {}).get("fields")
        if isinstance(fields_schema, dict):
            fields_schema.clear()
            fields_schema.update(
                {
                    "type": "object",
                    "properties": {
                        name: {"$ref": "#/$defs/FieldValue"} for name in FIELD_NAMES
                    },
                    "required": list(FIELD_NAMES),
                    "additionalProperties": False,
                }
            )
    return generated


FIELD_NAMES = [
    "dateCreated", "submissionDeadlineDate", "submissionDeadlineTime", "tenderUrlSource",
    "federalLaw", "stateDefenseOrder", "tenderStatus", "tenderStatusNote", "tenderStatusReason",
    "tenderGroup", "initialPrice", "finalPrice", "resultDate", "contractDate", "deliveryType",
    "deliveryBatchDays", "deliveryDays", "deliveryDate", "paymentDelayDays", "lotDivisible",
    "deliveryNote", "counterpartyCode", "counterpartyName", "counterpartyInn", "counterpartyKpp",
    "counterpartyCkg", "counterpartyPotential", "deal", "contract", "counterpartyNote",
    "customerContactPerson", "op", "legalEntity", "tenderSubmittedDate", "tenderWonDate",
    "applicationSecurity", "contractSecurity", "warrantySecurity", "warrantyMonths",
    "nationalRegime", "specialAccount",
]


FIELD_EXTRACTION_RULES = """
Ты извлекаешь поля для автозаполнения карточки тендера на сайте ЭТМ.
Верни только валидный JSON без markdown. Не придумывай значения. Если поле не найдено — value=null.
Для каждого поля верни {value, confidence: low|medium|high, source, evidence}.
Даты YYYY-MM-DD, время HH:mm. Денежные значения — строка с суммой и валютой.
tenderStatus на извлечении по умолчанию «Загружен Seldon»; окончательное решение выполняется отдельно.
stateDefenseOrder=yes только при ГОЗ/275-ФЗ/отдельном счёте/казначейском сопровождении.
specialAccount=yes только при прямых признаках; иначе no.
lotDivisible только при прямой формулировке делимости/неделимости.
deliveryDate и deliveryDays заполняй только по прямому сроку поставки/доставки товара:
«срок поставки», «поставить до/не позднее», «доставка до», «в течение N дней».
Не используй для deliveryDate/deliveryDays срок действия договора, фразу «договор действует до»,
срок оплаты, гарантии, приёмки или окончания подачи заявок. Если прямого срока поставки нет — value=null.
counterparty — заказчик/получатель, не поставщик, банк, УФК или грузоотправитель.
op — внутреннее ОП ЭТМ, не контактное лицо заказчика.
legalEntity всегда null. Код ТО не извлекай: он приходит программно.
Приоритет: договор/ТЗ/спецификация, затем Seldon/страница.
""".strip()


class LlmClient:
    def __init__(
        self,
        settings: Settings,
        attempt: int,
        observer: "RunObserver | None" = None,
    ) -> None:
        self.settings = settings
        self.attempt = attempt
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY не настроен")
        self.model = settings.model_for_attempt(attempt)
        self.model_chain = settings.models_for_attempt(attempt)
        self.models_used: list[str] = []
        self.observer = observer
        self.client = OpenAI(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )

    def _uses_openrouter_extensions(self) -> bool:
        return self.settings.llm_provider == "openrouter"

    def _fallback_body(self, models: list[str]) -> dict[str, Any]:
        if not self._uses_openrouter_extensions() or len(models) < 2:
            return {}
        return {"models": models[1:]}

    def _structured_extra_body(self, models: list[str]) -> dict[str, Any]:
        body = self._fallback_body(models)
        if not self._uses_openrouter_extensions():
            return body
        if self.settings.llm_require_supported_parameters:
            body["provider"] = {"require_parameters": True}
        if self.settings.llm_enable_response_healing:
            body["plugins"] = [{"id": "response-healing"}]
        return body

    def _response_format(self, schema: type[T]) -> dict[str, Any]:
        if self.settings.llm_structured_output_mode == "json_object":
            return {"type": "json_object"}
        schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", schema.__name__)[:64]
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name or "structured_response",
                "strict": self.settings.llm_json_schema_strict,
                "schema": _response_schema(
                    schema,
                    strict=self.settings.llm_json_schema_strict,
                ),
            },
        }

    @staticmethod
    def _response_details(response: Any) -> dict[str, Any]:
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        content = str(getattr(message, "content", None) or "")
        return {
            "finishReason": getattr(choice, "finish_reason", None),
            "contentChars": len(content),
            "contentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content
            else None,
        }

    def _record_model(self, response: Any, primary: str) -> None:
        used = str(getattr(response, "model", None) or primary)
        if used not in self.models_used:
            self.models_used.append(used)

    @staticmethod
    def _usage(response: Any) -> tuple[int, int, int]:
        usage = getattr(response, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
        return prompt, completion, total

    def _observe_llm(
        self,
        *,
        operation: str,
        primary_model: str,
        model_chain: list[str],
        started: float,
        response: Any = None,
        error: BaseException | None = None,
        audit_details: dict[str, Any] | None = None,
    ) -> None:
        if not self.observer:
            return
        actual_model = str(getattr(response, "model", None) or primary_model)
        prompt_tokens, completion_tokens, total_tokens = self._usage(response)
        fallback_used = actual_model != primary_model
        counters = {
            "llm_requests": 1,
            "llm_successes" if error is None else "llm_failures": 1,
            "llm_prompt_tokens": prompt_tokens,
            "llm_completion_tokens": completion_tokens,
            "llm_total_tokens": total_tokens,
            "llm_fallbacks": 1 if fallback_used else 0,
        }
        self.observer.event(
            event_type="external_call",
            status="completed" if error is None else "failed",
            stage=operation,
            service="llm",
            operation=operation,
            model=actual_model,
            primary_model=primary_model,
            provider_request_id=str(getattr(response, "id", "") or "") or None,
            http_method="POST",
            http_status=int(getattr(error, "status_code", 0) or 0) or (200 if error is None else None),
            duration_seconds=round(time.monotonic() - started, 3),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error=error,
            details={
                "modelChain": model_chain,
                "fallbackUsed": fallback_used,
                "attempt": self.attempt,
                **(self._response_details(response) if response is not None else {}),
                **(audit_details or {}),
            },
            counters=counters,
        )

    def _observe_parse(
        self,
        *,
        operation: str,
        response: Any,
        started: float,
        extraction: JsonExtractionResult | None = None,
        fields_shape_normalized: bool = False,
        products_shape_normalized: bool = False,
        error: BaseException | None = None,
    ) -> None:
        if not self.observer:
            return
        initial_error = extraction.initial_error if extraction else None
        details: dict[str, Any] = {
            **self._response_details(response),
            "jsonSource": extraction.source if extraction else None,
            "jsonRepaired": bool(extraction and extraction.repaired),
            "fieldsShapeNormalized": fields_shape_normalized,
            "productsShapeNormalized": products_shape_normalized,
        }
        if initial_error is not None:
            details.update(
                {
                    "initialJsonErrorLine": initial_error.lineno,
                    "initialJsonErrorColumn": initial_error.colno,
                    "initialJsonErrorPosition": initial_error.pos,
                }
            )
        if isinstance(error, json.JSONDecodeError):
            details.update(
                {
                    "jsonErrorLine": error.lineno,
                    "jsonErrorColumn": error.colno,
                    "jsonErrorPosition": error.pos,
                }
            )
        self.observer.event(
            event_type="llm_response_parse",
            status="failed" if error else "completed",
            stage=operation,
            service="llm",
            operation=f"{operation}_parse",
            model=str(getattr(response, "model", None) or self.model),
            primary_model=self.model,
            duration_seconds=round(time.monotonic() - started, 3),
            error=error,
            details=details,
        )

    def json_call(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        operation: str = "structured_json",
        audit_details: dict[str, Any] | None = None,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                max_tokens=self.settings.llm_max_output_tokens,
                response_format=self._response_format(schema),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{prompt}\n\nJSON Schema:\n{schema_json}"},
                ],
                extra_body=self._structured_extra_body(self.model_chain),
            )
        except Exception as exc:
            self._observe_llm(
                operation=operation,
                primary_model=self.model,
                model_chain=self.model_chain,
                started=started,
                error=exc,
                audit_details=audit_details,
            )
            raise
        self._record_model(response, self.model)
        self._observe_llm(
            operation=operation,
            primary_model=self.model,
            model_chain=self.model_chain,
            started=started,
            response=response,
            audit_details=audit_details,
        )
        content = response.choices[0].message.content or ""
        parse_started = time.monotonic()
        extraction: JsonExtractionResult | None = None
        fields_shape_normalized = False
        products_shape_normalized = False
        try:
            finish_reason = self._response_details(response).get("finishReason")
            normalized_finish_reason = str(finish_reason or "").strip().lower()
            if normalized_finish_reason in {"length", "max_tokens", "max_output_tokens"}:
                raise LlmResponseTruncatedError(
                    f"LLM-ответ обрезан провайдером: finish_reason={finish_reason}"
                )
            extraction = extract_json_result(
                content,
                # Repairing a response explicitly marked as truncated could turn an
                # incomplete business decision into an apparently valid one.
                allow_repair=True,
            )
            fields_shape_normalized = bool(
                schema is ExtractedFieldsResponse
                and isinstance(extraction.value, dict)
                and isinstance(extraction.value.get("fields"), list)
            )
            validation_value: Any = extraction.value
            if schema is TenderPositionsResponse and isinstance(validation_value, list):
                validation_value = {"products": validation_value, "warnings": []}
                products_shape_normalized = True
            validated = schema.model_validate(validation_value)
        except Exception as exc:
            self._observe_parse(
                operation=operation,
                response=response,
                started=parse_started,
                extraction=extraction,
                fields_shape_normalized=fields_shape_normalized,
                products_shape_normalized=products_shape_normalized,
                error=exc,
            )
            raise
        self._observe_parse(
            operation=operation,
            response=response,
            started=parse_started,
            extraction=extraction,
            fields_shape_normalized=fields_shape_normalized,
            products_shape_normalized=products_shape_normalized,
        )
        return validated

    def extract_fields(self, combined_text: str) -> ExtractedFieldsResponse:
        skeleton = {name: {"value": None, "confidence": "low", "source": None, "evidence": None} for name in FIELD_NAMES}
        prompt = (
            f"{FIELD_EXTRACTION_RULES}\n\nJSON ответа: {{\"fields\": ..., \"warnings\": []}}\n"
            f"Ожидаемые поля:\n{json.dumps(skeleton, ensure_ascii=False)}\n\n"
            f"Текст для анализа:\n{combined_text}"
        )
        return self.json_call(
            system="Ты извлекаешь структурированные данные из тендерной документации. Только JSON.",
            prompt=prompt,
            schema=ExtractedFieldsResponse,
            operation="extract_tender_fields",
        )

    def extract_products(self, text: str, deterministic: list[dict[str, Any]]) -> TenderPositionsResponse:
        source_text = text[: self.settings.max_product_text_chars]

        def build_prompt(
            text_part: str,
            deterministic_part: list[dict[str, Any]],
        ) -> str:
            return f"""
Извлеки товарные позиции из тендерной документации. Верни только JSON.
Используй ТЗ, спецификацию, ведомость и таблицы. Каждая строка после колонок
«№ п/п / Наименование / Ед. изм. / Кол-во» — товар. Не пропускай quantity.
Не превращай заголовки, реквизиты, услуги площадки и обеспечения в товары.
Не превращай в товары адреса поставки, почтовые адреса, названия заказчиков,
получателей, грузополучателей, филиалов и производственных площадок.
Не превращай в товары служебные значения отдельных ячеек: «ОЛ-5», «ОЛ-6»,
«ОЛ-7», «Пример», номера строк, коды классификаторов и подписи образца заполнения.
Сохрани полное наименование, характеристики, brand/article, quantity и unit.
quantity бери только из колонки количества/«Кол-во». Никогда не используй как
quantity цену единицы, стоимость строки, НМЦ или другое число из удалённой колонки.
Для Excel сверяй колонку единицы измерения и соседнюю колонку количества.
analogsAllowed=false при «без аналогов/эквивалент не допускается»; true при прямом разрешении.
Для каждой позиции отдельно извлеки documentUnitPriceRub (цена одной единицы) и
documentLineTotalRub (сумма/стоимость всей строки), только когда соответствующий смысл
явно подтверждён заголовком таблицы или текстом. Не путай сумму строки с ценой единицы.
Если дана только сумма строки, не дели её самостоятельно: верни documentLineTotalRub,
а documentUnitPriceRub оставь null. Валюту верни в documentCurrency (RUB и т. п.).
В documentPriceEvidence сохрани короткий фрагмент с ценой, количеством и заголовком.
Для цены, найденной моделью, укажи documentPriceSource.extractionMethod="llm" и по
возможности fileName/sheet/row; неизвестные поля источника оставь пустыми/null.
Цена из документа является только диагностикой и не заменяет цену каталога/Qdrant.
Начальная цена тендера, НМЦ или НМЦК относится ко всему тендеру: никогда не записывай
её в documentUnitPriceRub или documentLineTotalRub отдельной товарной позиции.
Не изменяй детерминированные Excel-цены и их documentPriceSource.
Перед формированием ответа выполни итоговую сверку всех извлечённых строк:
- одна и та же позиция может повторяться в ТЗ, спецификации, ведомости и проекте договора;
- сопоставляй дубли по номеру позиции, артикулу/коду, базовому наименованию и quantity;
- длинное наименование с характеристиками и короткое наименование того же товара — одна позиция;
- при объединении сохрани наиболее полные requirements/evidence, quantity и источник цены;
- не суммируй quantity, если это одно и то же требование, повторённое в разных документах;
- в products верни каждую уникальную товарную позицию ровно один раз.
Максимум 100 уникальных позиций после этой проверки.

Детерминированные Excel-позиции — обязательная основа:
{json.dumps(deterministic_part, ensure_ascii=False, indent=2)}

Текст:
{text_part}
""".strip()

        should_chunk_immediately = (
            len(source_text) > PRODUCT_DIRECT_CALL_MAX_CHARS
            or len(deterministic) > 25
        )
        warnings: list[str] = []
        if not should_chunk_immediately:
            try:
                return self.json_call(
                    system="Ты извлекаешь товарные позиции. Только валидный JSON.",
                    prompt=build_prompt(source_text, deterministic),
                    schema=TenderPositionsResponse,
                    operation="extract_tender_products",
                )
            except LlmResponseTruncatedError:
                warnings.append(
                "Полный ответ LLM по товарным позициям был обрезан; "
                "извлечение автоматически повторено частями."
                )
        else:
            warnings.append(
                "Большой текст или большой детерминированный список позиций: "
                "LLM-извлечение сразу выполнено небольшими частями."
            )

        # Deterministic Excel positions are merged by the pipeline after this call,
        # so repeating their complete JSON in every LLM chunk only wastes context
        # and output tokens. Each small text chunk extracts its own visible rows.
        work: list[tuple[str, int]] = [
            (chunk, 0)
            for chunk in _split_product_text(source_text)
        ]
        if not work:
            return TenderPositionsResponse(products=[], warnings=warnings)

        products = []
        call_number = 0
        while work:
            text_part, depth = work.pop(0)
            call_number += 1
            try:
                response = self.json_call(
                    system="Ты извлекаешь товарные позиции. Только валидный JSON.",
                    prompt=build_prompt(text_part, []),
                    schema=TenderPositionsResponse,
                    operation=f"extract_tender_products_chunk_{call_number}",
                )
            except LlmResponseTruncatedError:
                if depth >= PRODUCT_CHUNK_MAX_DEPTH:
                    raise
                text_chunks = _split_product_text(
                    text_part,
                    target_chars=max(1_000, len(text_part) // 2),
                )
                if len(text_chunks) < 2:
                    raise
                work[0:0] = [
                    (chunk, depth + 1)
                    for chunk in text_chunks
                ]
                continue
            products.extend(response.products)
            warnings.extend(response.warnings)

        unique_products = []
        seen: set[tuple[str, float | None, str]] = set()
        for product in products:
            key = (
                re.sub(
                    r"[^a-zа-яё0-9]+",
                    " ",
                    str(product.productQuery or product.product).casefold(),
                ).strip(),
                product.quantity,
                product.unit.casefold(),
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            unique_products.append(product)
            if len(unique_products) >= 100:
                warnings.append(
                    "После частичного извлечения достигнут лимит 100 уникальных позиций."
                )
                break
        return TenderPositionsResponse(
            products=unique_products,
            warnings=list(dict.fromkeys(warnings)),
        )

    def decide(self, prompt: str) -> LlmDecision:
        return self.json_call(
            system=(
                "Ты проверяешь тендер по утвержденному справочнику причин отказа. "
                "Не отменяй детерминированные стоп-факторы и не придумывай evidence. Только JSON."
            ),
            prompt=prompt,
            schema=LlmDecision,
            operation="decide_tender_status",
        )

    def ocr_pdf(self, path: Path) -> str:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        ocr_models = self.settings.models_for_ocr()
        extra_body: dict[str, Any] = {
            "plugins": [{"id": "file-parser", "pdf": {"engine": self.settings.ocr_pdf_engine}}]
        }
        extra_body.update(self._fallback_body(ocr_models))
        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=ocr_models[0],
                temperature=0,
                max_tokens=self.settings.llm_max_output_tokens,
                messages=[
                    {
                        "role": "system",
                        "content": "Ты OCR-модуль. Верни только распознанный текст документа без markdown.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Распознай весь текст PDF, сохраняя таблицы и числа."},
                            {
                                "type": "file",
                                "file": {
                                    "filename": path.name,
                                    "file_data": f"data:application/pdf;base64,{data}",
                                },
                            },
                        ],
                    },
                ],
                extra_body=extra_body,
            )
        except Exception as exc:
            self._observe_llm(
                operation="ocr_pdf",
                primary_model=ocr_models[0],
                model_chain=ocr_models,
                started=started,
                error=exc,
            )
            raise
        self._record_model(response, ocr_models[0])
        self._observe_llm(
            operation="ocr_pdf",
            primary_model=ocr_models[0],
            model_chain=ocr_models,
            started=started,
            response=response,
        )
        return response.choices[0].message.content or ""
