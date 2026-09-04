from __future__ import annotations

import base64
import copy
import hashlib
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from json_repair import repair_json
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.models import (
    DocumentAnalysisResponse,
    DocumentAnalysisUnit,
    ExtractedFieldsResponse,
    LlmDecision,
    ProductCandidateAuditResponse,
    ProductHierarchyResponse,
    SpreadsheetCandidateReviewResponse,
    TenderConsolidationResponse,
    TenderPosition,
    TenderPositionsResponse,
)

if TYPE_CHECKING:
    from app.observability import RunObserver


T = TypeVar("T", bound=BaseModel)


class LlmWallTimeoutError(TimeoutError):
    pass


@dataclass(frozen=True)
class JsonExtractionResult:
    value: dict[str, Any] | list[Any]
    source: str
    repaired: bool = False
    initial_error: json.JSONDecodeError | None = None


class LlmResponseTruncatedError(RuntimeError):
    """The provider stopped before returning a complete structured response."""


class LlmMalformedResponseError(RuntimeError):
    pass


PRODUCT_DIRECT_CALL_MAX_CHARS = 30_000
PRODUCT_LLM_SKIP_DETERMINISTIC_COUNT = 25


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

    def _call_with_wall_timeout(self, operation: str, timeout_seconds: float | None, call: Any) -> Any:
        if timeout_seconds is None:
            return call()
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def runner() -> None:
            try:
                result_queue.put(("ok", call()))
            except BaseException as exc:
                result_queue.put(("error", exc))

        thread = threading.Thread(target=runner, name=f"llm-{operation}", daemon=True)
        thread.start()
        try:
            kind, payload = result_queue.get(timeout=float(timeout_seconds))
        except queue.Empty as exc:
            raise LlmWallTimeoutError(
                f"LLM operation {operation} exceeded wall timeout {timeout_seconds} seconds"
            ) from exc
        if kind == "error":
            raise payload
        return payload

    def _rate_limit_backoff(self, model_index: int) -> None:
        delay = float(getattr(self.settings, "llm_rate_limit_backoff_seconds", 0) or 0)
        if delay > 0:
            time.sleep(delay * max(1, model_index + 1))

    @staticmethod
    def _is_rate_limit_error(error: BaseException) -> bool:
        status_code = int(getattr(error, "status_code", 0) or 0)
        return status_code == 429 or "ratelimit" in type(error).__name__.lower()

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
        reasoning_effort = self.settings.llm_reasoning_effort
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
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
            "outputChars": len(content),
            "contentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content
            else None,
        }

    def _record_model(self, response: Any, primary: str) -> None:
        used = str(getattr(response, "model", None) or primary)
        if used not in self.models_used:
            self.models_used.append(used)

    @staticmethod
    def _response_content(response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            raise LlmMalformedResponseError("LLM response did not include choices")
        choice = choices[0]
        if choice is None:
            raise LlmMalformedResponseError("LLM response first choice is empty")
        message = getattr(choice, "message", None)
        if message is None:
            raise LlmMalformedResponseError("LLM response choice did not include message")
        return str(getattr(message, "content", None) or "")

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
        configured_max_completion_tokens: int | None = None,
        input_chars: int | None = None,
        schema_chars: int | None = None,
        input_sha256: str | None = None,
        logical_call_id: str | None = None,
        physical_call_index: int = 1,
        timeout_seconds: float | None = None,
        configured_max_attempts: int | None = None,
    ) -> None:
        if not self.observer:
            return
        actual_model = str(getattr(response, "model", None) or primary_model)
        prompt_tokens, completion_tokens, total_tokens = self._usage(response)
        fallback_used = physical_call_index > 1 or actual_model != primary_model
        duration = round(time.monotonic() - started, 3)
        truncated = isinstance(error, LlmResponseTruncatedError)
        status_code = int(getattr(error, "status_code", 0) or 0) if error else 200
        rate_limited = status_code == 429
        timeout_error = error is not None and "timeout" in type(error).__name__.lower()
        counters = {
            "llm_requests": 1,
            "llm_successes" if error is None else "llm_failures": 1,
            "llm_prompt_tokens": prompt_tokens,
            "llm_completion_tokens": completion_tokens,
            "llm_total_tokens": total_tokens,
            "llm_fallbacks": 1 if fallback_used else 0,
        }
        llm_performance = {
            "logicalCalls": 1 if physical_call_index == 1 else 0,
            "physicalCalls": 1,
            "fallbackCalls": 1 if fallback_used else 0,
            "retriedCalls": 0,
            "successfulCalls": 1 if error is None else 0,
            "failedCalls": 1 if error is not None else 0,
            "truncatedCalls": 1 if truncated else 0,
            "rateLimitedCalls": 1 if rate_limited else 0,
            "timeoutCalls": 1 if timeout_error else 0,
            "totalLlmSeconds": duration,
            "successfulLlmSeconds": duration if error is None else 0,
            "failedLlmSeconds": duration if error is not None else 0,
            "fallbackLlmSeconds": duration if fallback_used else 0,
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
            http_status=status_code or None,
            duration_seconds=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error=error,
            details={
                "modelChain": model_chain,
                "fallbackUsed": fallback_used,
                "attempt": self.attempt,
                "jobAttempt": self.attempt,
                "logicalCallId": logical_call_id,
                "physicalCallIndex": physical_call_index,
                "physicalCallBudget": len(model_chain),
                "inputSha256": input_sha256,
                "inputChars": input_chars,
                "schemaChars": schema_chars,
                "configuredMaxCompletionTokens": configured_max_completion_tokens,
                "timeoutSeconds": timeout_seconds,
                "configuredMaxAttemptsPerUnit": configured_max_attempts,
                "actualPromptTokens": prompt_tokens,
                "actualCompletionTokens": completion_tokens,
                "actualTotalTokens": total_tokens,
                "llmPerformance": llm_performance,
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
        primary_model: str | None = None,
        configured_max_completion_tokens: int | None = None,
        input_chars: int | None = None,
        schema_chars: int | None = None,
        input_sha256: str | None = None,
        logical_call_id: str | None = None,
        timeout_seconds: float | None = None,
        configured_max_attempts: int | None = None,
    ) -> None:
        if not self.observer:
            return
        initial_error = extraction.initial_error if extraction else None
        prompt_tokens, completion_tokens, total_tokens = self._usage(response)
        details: dict[str, Any] = {
            **self._response_details(response),
            "jsonSource": extraction.source if extraction else None,
            "jsonRepaired": bool(extraction and extraction.repaired),
            "fieldsShapeNormalized": fields_shape_normalized,
            "productsShapeNormalized": products_shape_normalized,
            "attempt": self.attempt,
            "jobAttempt": self.attempt,
            "logicalCallId": logical_call_id,
            "inputSha256": input_sha256,
            "inputChars": input_chars,
            "schemaChars": schema_chars,
            "configuredMaxCompletionTokens": configured_max_completion_tokens,
            "timeoutSeconds": timeout_seconds,
            "configuredMaxAttemptsPerUnit": configured_max_attempts,
            "actualPromptTokens": prompt_tokens,
            "actualCompletionTokens": completion_tokens,
            "actualTotalTokens": total_tokens,
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
        if error is not None:
            choices = getattr(response, "choices", None) or []
            choice = choices[0] if choices else None
            message = getattr(choice, "message", None)
            content = str(getattr(message, "content", None) or "")
            details["contentPreview"] = content[:2000]
        self.observer.event(
            event_type="llm_response_parse",
            status="failed" if error else "completed",
            stage=operation,
            service="llm",
            operation=f"{operation}_parse",
            model=str(getattr(response, "model", None) or self.model),
            primary_model=primary_model or self.model,
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
        model_chain: list[str] | None = None,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        max_completion_tokens = self.settings.max_completion_tokens_for(operation)
        request_timeout = self.settings.timeout_for(operation) or self.settings.llm_timeout_seconds
        thinking_hint = "/no_think\n" if self.settings.llm_reasoning_effort == "none" else ""
        request_payload = f"{system}\n\n{thinking_hint}{prompt}\n\nJSON Schema:\n{schema_json}"
        input_sha256 = hashlib.sha256(request_payload.encode("utf-8")).hexdigest()
        logical_call_id = hashlib.sha256(
            f"{operation}\n{schema.__name__}\n{input_sha256}".encode("utf-8")
        ).hexdigest()
        input_chars = len(prompt) + len(system)
        schema_chars = len(schema_json)
        last_error: Exception | None = None
        max_attempts = max(1, int(self.settings.llm_max_attempts_per_unit))
        models = list(model_chain or self.model_chain or [self.model])[:max_attempts]
        primary_model = models[0]
        for index, model in enumerate(models):
            started = time.monotonic()
            physical_call_index = index + 1
            retry_hint = (
                "\n\nPrevious model response was not accepted as valid JSON. "
                "Return only one valid JSON object that matches the schema, without markdown, comments, or prose."
                if index
                else ""
            )
            try:
                response = self._call_with_wall_timeout(
                    operation,
                    request_timeout,
                    lambda model=model, retry_hint=retry_hint: self.client.chat.completions.create(
                        model=model,
                        temperature=0,
                        max_tokens=max_completion_tokens,
                        timeout=request_timeout,
                        response_format=self._response_format(schema),
                        messages=[
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": f"{thinking_hint}{prompt}{retry_hint}\n\nJSON Schema:\n{schema_json}",
                            },
                        ],
                        extra_body=self._structured_extra_body([model]),
                    ),
                )
            except Exception as exc:
                last_error = exc
                self._observe_llm(
                    operation=operation,
                    primary_model=primary_model,
                    model_chain=models,
                    started=started,
                    error=exc,
                    audit_details=audit_details,
                    configured_max_completion_tokens=max_completion_tokens,
                    input_chars=input_chars,
                    schema_chars=schema_chars,
                    input_sha256=input_sha256,
                    logical_call_id=logical_call_id,
                    physical_call_index=physical_call_index,
                    timeout_seconds=request_timeout,
                    configured_max_attempts=max_attempts,
                )
                if index + 1 < len(models):
                    if self._is_rate_limit_error(exc):
                        self._rate_limit_backoff(index)
                    continue
                raise

            self._record_model(response, model)
            parse_started = time.monotonic()
            extraction: JsonExtractionResult | None = None
            fields_shape_normalized = False
            products_shape_normalized = False
            try:
                finish_reason = self._response_details(response).get("finishReason")
                normalized_finish_reason = str(finish_reason or "").strip().lower()
                if normalized_finish_reason in {"length", "max_tokens", "max_output_tokens"}:
                    raise LlmResponseTruncatedError(
                        f"LLM response was truncated by provider: finish_reason={finish_reason}"
                    )
                content = self._response_content(response)
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
                last_error = exc
                self._observe_parse(
                    operation=operation,
                    response=response,
                    started=parse_started,
                    extraction=extraction,
                    fields_shape_normalized=fields_shape_normalized,
                    products_shape_normalized=products_shape_normalized,
                    error=exc,
                    primary_model=primary_model,
                    configured_max_completion_tokens=max_completion_tokens,
                    input_chars=input_chars,
                    schema_chars=schema_chars,
                    input_sha256=input_sha256,
                    logical_call_id=logical_call_id,
                    timeout_seconds=request_timeout,
                    configured_max_attempts=max_attempts,
                )
                self._observe_llm(
                    operation=operation,
                    primary_model=primary_model,
                    model_chain=models,
                    started=started,
                    response=response,
                    error=exc,
                    audit_details=audit_details,
                    configured_max_completion_tokens=max_completion_tokens,
                    input_chars=input_chars,
                    schema_chars=schema_chars,
                    input_sha256=input_sha256,
                    logical_call_id=logical_call_id,
                    physical_call_index=physical_call_index,
                    timeout_seconds=request_timeout,
                    configured_max_attempts=max_attempts,
                )
                if index + 1 < len(models):
                    if self._is_rate_limit_error(exc):
                        self._rate_limit_backoff(index)
                    continue
                raise

            self._observe_parse(
                operation=operation,
                response=response,
                started=parse_started,
                extraction=extraction,
                fields_shape_normalized=fields_shape_normalized,
                products_shape_normalized=products_shape_normalized,
                primary_model=primary_model,
                configured_max_completion_tokens=max_completion_tokens,
                input_chars=input_chars,
                schema_chars=schema_chars,
                input_sha256=input_sha256,
                logical_call_id=logical_call_id,
                timeout_seconds=request_timeout,
                configured_max_attempts=max_attempts,
            )
            self._observe_llm(
                operation=operation,
                primary_model=primary_model,
                model_chain=models,
                started=started,
                response=response,
                audit_details=audit_details,
                configured_max_completion_tokens=max_completion_tokens,
                input_chars=input_chars,
                schema_chars=schema_chars,
                input_sha256=input_sha256,
                logical_call_id=logical_call_id,
                physical_call_index=physical_call_index,
                timeout_seconds=request_timeout,
                configured_max_attempts=max_attempts,
            )
            return validated

        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM model chain is empty")

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

    def extract_products(
        self,
        text: str,
        deterministic: list[dict[str, Any]],
        *,
        trust_deterministic: bool = False,
    ) -> TenderPositionsResponse:
        source_text = text[: self.settings.max_product_text_chars]
        if trust_deterministic and deterministic:
            return TenderPositionsResponse(
                products=[],
                warnings=[
                    "Deterministic spreadsheet product rows are used as the source of truth; "
                    "skipped LLM product extraction to avoid duplicate rows and long model calls."
                ],
            )
        if len(deterministic) >= PRODUCT_LLM_SKIP_DETERMINISTIC_COUNT:
            return TenderPositionsResponse(
                products=[],
                warnings=[
                    "Deterministic Excel already produced a large product list; "
                    "skipped full-text LLM product extraction to avoid duplicate rows and long model calls."
                ],
            )

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
Товаром из Excel может стать только значение из колонки наименования товара/продукции.
Ячейки характеристик, адресов, кодов, цен и условий поставки добавляй в evidence или
requirements, но не создавай из них отдельные products.
Для каждой позиции из Excel заполни sourceReference: fileName, sheet, row,
productColumn, quantityColumn, unitColumn и extractionMethod="llm". Не придумывай
координаты: если их нет в тексте, оставь соответствующие поля пустыми/null.
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


        if len(source_text) > PRODUCT_DIRECT_CALL_MAX_CHARS:
            return TenderPositionsResponse(
                products=[],
                warnings=[
                    "Product extraction text is too large for safe single-call LLM extraction; "
                    "skipped LLM product extraction instead of splitting into chunks."
                ],
            )

        try:
            return self.json_call(
                system="Ты извлекаешь товарные позиции. Только валидный JSON.",
                prompt=build_prompt(source_text, deterministic),
                schema=TenderPositionsResponse,
                operation="extract_tender_products",
            )
        except (LlmResponseTruncatedError, LlmMalformedResponseError, OpenAIError, ValidationError) as exc:
            return TenderPositionsResponse(
                products=[],
                warnings=[
                    "LLM product extraction returned an unsafe response; "
                    f"skipped chunk retry and continued with deterministic/Seldon positions. Error: {exc}"
                ],
            )


    def review_spreadsheet_candidates(
        self,
        positions: list[TenderPosition],
    ) -> SpreadsheetCandidateReviewResponse:
        items = [
            {
                "candidateId": position.candidateId,
                "product": position.product,
                "productQuery": position.productQuery,
                "quantity": position.quantity,
                "unit": position.unit,
                "sourceReference": (
                    position.sourceReference.model_dump()
                    if position.sourceReference is not None
                    else None
                ),
                "sourceCells": position.sourceCells,
                "evidence": position.evidence[:300],
            }
            for position in positions
            if position.candidateId
        ]
        prompt = f"""
Проверь deterministic Excel candidates. Верни только JSON.

Твоя задача — не извлекать таблицу заново и не переписывать числа/цены/координаты.
Python уже знает quantity, unit, prices, sourceCells и sourceReference.
Для каждого candidateId верни ровно одно решение:
- KEEP: это настоящая закупаемая товарная позиция;
- CORRECT: это товар, но название нужно нормализовать; укажи normalizedProduct;
- REMOVE: это адрес, число, заголовок, характеристика, служебная строка, компонент без доказательства самостоятельной поставки или дубль;
- NEW: только если в sourceCells явно есть реальная закупаемая позиция, которую candidate product пропустил; укажи normalizedProduct.

Правила:
- Не превращай quantity/unit/price/адрес/слово «преимущество»/заголовки в товары.
- Дубль одной и той же модели/позиции удаляй через REMOVE и duplicateOfCandidateId.
- Повтор одной позиции в ТЗ/договоре/спецификации не увеличивает количество.
- При конфликте количества не удаляй автоматически: KEEP/CORRECT с reason о конфликте.
- reason короткий, до 300 символов. Не копируй всю строку.

Candidates:
{json.dumps(items, ensure_ascii=False, indent=2)}
""".strip()
        return self.json_call(
            system=(
                "Ты классифицируешь Excel candidates по candidateId. "
                "Не генерируй заново metadata таблицы. Только JSON."
            ),
            prompt=prompt,
            schema=SpreadsheetCandidateReviewResponse,
            operation="analyze_document: spreadsheet_candidate_review",
        )

    def analyze_document_unit(
        self,
        unit: DocumentAnalysisUnit,
    ) -> DocumentAnalysisResponse:
        prompt = f"""
Проанализируй один DocumentAnalysisUnit. Верни только компактный JSON.

Источник:
- unitId: {unit.unitId}
- sourceType: {unit.sourceType}
- fileName: {unit.fileName}
- documentKind: {unit.documentKind}
- part: {unit.partIndex}/{unit.partTotal}
- inputSha256: {unit.inputSha256}

Нужно извлечь только факты из этого unit:
1. products — закупаемые товарные позиции из текста. Evidence до 500 символов.
2. reasonHits — только реально подтверждённые недетерминированные причины отказа. Не возвращай false для остальных причин.
3. fieldCandidates — найденные поля карточки тендера, если они явно есть в этом unit.

Ограничения:
- Не принимай финальное решение по тендеру.
- Верни не более 25 products, 20 reasonHits и 30 fieldCandidates для одного unit.
- Если фактов больше, выбери самые важные, поставь analysisIncomplete=true и добавь короткое warning.
- Evidence держи коротким: только фрагмент-доказательство, не копируй большие абзацы.
- Не считай инструкцию/руководство/документацию по монтажу, наладке или пуску работами.
- Причина «Номенклатура. Поставка с работами» только при прямой обязанности поставщика выполнить монтаж/установку/ПНР/шефмонтаж/ввод в эксплуатацию.
- ЗИП/ремкомплект/запасные части — reasonHit, если физический комплект/запчасти входят в поставку или сама позиция является ЗИП.
- Не возвращай coverage, цены каталога, final status, полный справочник причин или raw pages.
- Для Excel не повторяй deterministic metadata; если candidates переданы, опирайся на candidateId.

Spreadsheet candidates, если есть:
{json.dumps(unit.spreadsheetCandidates, ensure_ascii=False, indent=2)}

Текст unit:
{unit.text}
""".strip()
        return self.json_call(
            system=(
                "Ты Document Analyzer. Читаешь ровно один bounded unit и извлекаешь "
                "products, reasonHits, fieldCandidates. Не принимаешь финальное решение. Только JSON."
            ),
            prompt=prompt,
            schema=DocumentAnalysisResponse,
            operation=f"analyze_document: {unit.fileName or unit.sourceType} part {unit.partIndex}/{unit.partTotal}",
            audit_details={
                "unitId": unit.unitId,
                "unitInputSha256": unit.inputSha256,
                "sourceType": unit.sourceType,
                "fileName": unit.fileName,
                "partIndex": unit.partIndex,
                "partTotal": unit.partTotal,
            },
        )

    def consolidate_document_analysis(
        self,
        results: list[dict[str, Any]],
    ) -> TenderConsolidationResponse:
        compact = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
        prompt = f"""
Сконсолидируй результаты Document Analysis. Только JSON.

Вход уже structured и compact. Raw document text тебе не передан.
Задачи:
- объединить products;
- удалить междокументные дубли;
- удалить мусорные товары: адреса, числа, заголовки, характеристики, служебные строки;
- сохранить quantity conflict как warning/incomplete, а не молча суммировать;
- объединить reasonHits и fieldCandidates;
- не считать coverage, Qdrant selection, final status или причины по каталогу.

DocumentAnalysisResults:
{compact}
""".strip()
        return self.json_call(
            system=(
                "Ты Tender Consolidator. Получаешь только structured facts, не raw text. "
                "Не принимаешь финальное решение. Только JSON."
            ),
            prompt=prompt,
            schema=TenderConsolidationResponse,
            operation="consolidate_tender_analysis",
        )


    def audit_product_candidates(
        self,
        positions: list[TenderPosition],
    ) -> ProductCandidateAuditResponse:
        items = [
            {
                "positionIndex": index,
                "product": position.product,
                "productQuery": position.productQuery,
                "article": position.article,
                "quantity": position.quantity,
                "unit": position.unit,
                "source": position.source,
                "sourceReference": (
                    position.sourceReference.model_dump()
                    if position.sourceReference is not None
                    else None
                ),
                "sourceCells": position.sourceCells,
                "requirements": position.requirements[:800],
                "evidence": position.evidence[:800],
            }
            for index, position in enumerate(positions, start=1)
        ]
        prompt = f"""
Проведи финальный аудит кандидатов на товарные позиции до поиска в каталоге.
Верни назначение ровно для каждого positionIndex и только JSON.

Допустимые роли:
- purchase_item: самостоятельно закупаемый и поставляемый товар;
- component: составная часть комплектного изделия, а не отдельный предмет поставки;
- characteristic: характеристика, значение параметра, код классификатора или отдельная ячейка;
- address: адрес, получатель, филиал или место поставки;
- service: условие закупки, служебная фраза, работа или услуга, не являющаяся товаром;
- header: заголовок или подпись таблицы;
- duplicate: повтор уже представленной товарной позиции;
- ambiguous: доказательств недостаточно.

Правила:
- Не считай названием товара фразы «аналоги рассматриваются», «эквиваленты допускаются»,
  адреса, номера строк, значения характеристик и коды без товарного наименования.
- Для duplicate укажи duplicateOf на более раннюю позицию. Повтор одного требования в ТЗ,
  спецификации и проекте договора не является новой поставкой; quantity не суммируй.
- Одинаковая модель в коротком и полном названии является сильным признаком duplicate,
  включая слитные коды без дефиса, например FPL1014.
- Одинаковый товар в разных лотах или явно разных строках поставки не объединяй без доказательств.
- Разное quantity у похожих позиций — конфликт, а не основание удалить одну из них;
  используй ambiguous, если источник не разрешает конфликт.
- Для component обязательно укажи parentPositionIndex. Используй component только когда
  контекст прямо показывает состав/комплектность родительского изделия.
- canonicalName заполняй только для безопасной очистки названия, не меняя модель, артикул
  и технические параметры.
- sourceReference и evidence являются доказательствами. Отсутствие координат Excel снижает
  уверенность; не придумывай координаты.
- Для Excel используй sourceCells как исходную JSON-строку таблицы. Значение из
  sourceReference.productColumn является кандидатом в товар; значения из quantityColumn,
  unitColumn и остальных колонок нельзя превращать в самостоятельные товары.

Кандидаты:
{json.dumps(items, ensure_ascii=False, indent=2)}
""".strip()
        return self.json_call(
            system=(
                "Ты проверяешь качество извлечения товарных позиций тендера. "
                "Не ищи товары и не принимай решение по тендеру. Только JSON."
            ),
            prompt=prompt,
            schema=ProductCandidateAuditResponse,
            operation="audit_product_candidates",
        )

    def classify_product_hierarchy(
        self,
        positions: list[TenderPosition],
    ) -> ProductHierarchyResponse:
        items = [
            {
                "positionIndex": index,
                "product": position.product,
                "quantity": position.quantity,
                "unit": position.unit,
                "source": position.source,
                "requirements": position.requirements[:600],
                "evidence": position.evidence[:600],
            }
            for index, position in enumerate(positions, start=1)
        ]
        prompt = f"""
Classify the extracted tender rows into purchase items and components.
Return one assignment for every input positionIndex and JSON only.

Rules:
- purchase_item is an independently purchased/delivered product that must be
  searched in the catalog and counted in coverage;
- component is included inside another listed purchase item (BOM,
  completeness, package contents) and must reference parentPositionIndex;
- ambiguous is used whenever the evidence is insufficient;
- never mark a separately requested good as a component only because it could
  technically be used inside another product;
- quantity, unit price, or a separate table row is not by itself proof that a
  row is an independent purchase item or a component;
- for KTP/complete transformer substations, transformers, switchgear,
  disconnectors, cabinets, relays and similar BOM rows can be components when
  the text shows that they form the listed KTP;
- be conservative: a mistaken component classification removes a row from
  catalog search, so use component only with direct contextual evidence;
- confidence must be between 0 and 1; include a concise rationale.

Input positions:
{json.dumps(items, ensure_ascii=False, indent=2)}
""".strip()
        return self.json_call(
            system=(
                "You classify parent purchase items and their included "
                "components in tender specifications. JSON only."
            ),
            prompt=prompt,
            schema=ProductHierarchyResponse,
            operation="classify_product_hierarchy",
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
        ocr_models = self.settings.models_for_ocr()[: max(1, int(self.settings.llm_max_attempts_per_unit))]
        max_completion_tokens = self.settings.max_completion_tokens_for("ocr_pdf")
        request_timeout = self.settings.timeout_for("ocr_pdf") or self.settings.llm_timeout_seconds
        file_bytes = path.stat().st_size
        input_sha256 = hashlib.sha256(
            f"ocr_pdf\n{path.name}\n{file_bytes}".encode("utf-8")
        ).hexdigest()
        extra_body: dict[str, Any] = {
            "plugins": [{"id": "file-parser", "pdf": {"engine": self.settings.ocr_pdf_engine}}]
        }
        extra_body.update(self._fallback_body(ocr_models))
        started = time.monotonic()
        try:
            response = self._call_with_wall_timeout(
                "ocr_pdf",
                request_timeout,
                lambda: self.client.chat.completions.create(
                    model=ocr_models[0],
                    temperature=0,
                    max_tokens=max_completion_tokens,
                    timeout=request_timeout,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an OCR module. Return only recognized document text without markdown.",
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Recognize all PDF text, preserving tables and numbers."},
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
                ),
            )
        except Exception as exc:
            self._observe_llm(
                operation="ocr_pdf",
                primary_model=ocr_models[0],
                model_chain=ocr_models,
                started=started,
                error=exc,
                configured_max_completion_tokens=max_completion_tokens,
                input_chars=file_bytes,
                schema_chars=0,
                input_sha256=input_sha256,
                logical_call_id=input_sha256,
                physical_call_index=1,
                timeout_seconds=request_timeout,
                configured_max_attempts=len(ocr_models),
            )
            raise
        self._record_model(response, ocr_models[0])
        self._observe_llm(
            operation="ocr_pdf",
            primary_model=ocr_models[0],
            model_chain=ocr_models,
            started=started,
            response=response,
            configured_max_completion_tokens=max_completion_tokens,
            input_chars=file_bytes,
            schema_chars=0,
            input_sha256=input_sha256,
            logical_call_id=input_sha256,
            physical_call_index=1,
            timeout_seconds=request_timeout,
            configured_max_attempts=len(ocr_models),
        )
        return response.choices[0].message.content or ""
