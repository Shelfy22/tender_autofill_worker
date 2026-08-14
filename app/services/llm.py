from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from openai import OpenAI
from pydantic import BaseModel

from app.config import Settings
from app.models import ExtractedFieldsResponse, LlmDecision, TenderPositionsResponse

if TYPE_CHECKING:
    from app.observability import RunObserver


T = TypeVar("T", bound=BaseModel)


def extract_json(text: str) -> dict[str, Any] | list[Any]:
    source = str(text or "").strip()
    if not source:
        raise ValueError("LLM вернул пустой ответ")
    try:
        return json.loads(source)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", source, re.I)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    start, end = source.find("{"), source.rfind("}")
    if start >= 0 and end > start:
        return json.loads(source[start : end + 1])
    raise ValueError("LLM не вернул валидный JSON")


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

    def _record_model(self, response: Any, primary: str) -> None:
        used = str(self._response_value(response, "model") or primary)
        if used not in self.models_used:
            self.models_used.append(used)

    @staticmethod
    def _response_value(response: Any, key: str) -> Any:
        if isinstance(response, dict):
            return response.get(key)
        return getattr(response, key, None)

    @staticmethod
    def _usage(response: Any) -> tuple[int, int, int]:
        usage = LlmClient._response_value(response, "usage") or {}
        if isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion = int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            total = int(usage.get("total_tokens") or prompt + completion)
            return prompt, completion, total
        prompt = int(
            getattr(usage, "prompt_tokens", 0)
            or getattr(usage, "input_tokens", 0)
            or 0
        )
        completion = int(
            getattr(usage, "completion_tokens", 0)
            or getattr(usage, "output_tokens", 0)
            or 0
        )
        total = int(getattr(usage, "total_tokens", 0) or prompt + completion)
        return prompt, completion, total

    @staticmethod
    def _http_status(error: BaseException | None) -> int | None:
        if error is None:
            return 200
        direct = int(getattr(error, "status_code", 0) or 0)
        if direct:
            return direct
        response = getattr(error, "response", None)
        return int(getattr(response, "status_code", 0) or 0) or None

    @staticmethod
    def _supports_temperature(model: str) -> bool:
        return not model.lower().startswith(("gpt-5", "o1", "o3", "o4"))

    def _chat_completion_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_completion_tokens": self.settings.llm_max_output_tokens,
        }
        if self._supports_temperature(self.model):
            kwargs["temperature"] = 0
        return kwargs

    @staticmethod
    def _responses_output_text(response: dict[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str):
            return direct
        texts: list[str] = []
        for output in response.get("output") or []:
            if not isinstance(output, dict):
                continue
            for content in output.get("content") or []:
                if not isinstance(content, dict) or content.get("type") != "output_text":
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return "\n".join(texts)

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
        actual_model = str(self._response_value(response, "model") or primary_model)
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
            provider_request_id=str(self._response_value(response, "id") or "") or None,
            http_method="POST",
            http_status=self._http_status(error),
            duration_seconds=round(time.monotonic() - started, 3),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            error=error,
            details={
                "modelChain": model_chain,
                "fallbackUsed": fallback_used,
                "attempt": self.attempt,
                **(audit_details or {}),
            },
            counters=counters,
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
                **self._chat_completion_kwargs(),
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{prompt}\n\nJSON Schema:\n{schema_json}"},
                ],
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
        return schema.model_validate(extract_json(content))

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
        prompt = f"""
Извлеки товарные позиции из тендерной документации. Верни только JSON.
Используй ТЗ, спецификацию, ведомость и таблицы. Каждая строка после колонок
«№ п/п / Наименование / Ед. изм. / Кол-во» — товар. Не пропускай quantity.
Не превращай заголовки, реквизиты, услуги площадки и обеспечения в товары.
Сохрани полное наименование, характеристики, brand/article, quantity и unit.
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
Не изменяй детерминированные Excel-цены и их documentPriceSource.
Максимум 100 уникальных позиций.

Детерминированные Excel-позиции — обязательная основа:
{json.dumps(deterministic, ensure_ascii=False, indent=2)}

Текст:
{text[: self.settings.max_product_text_chars]}
""".strip()
        return self.json_call(
            system="Ты извлекаешь товарные позиции. Только валидный JSON.",
            prompt=prompt,
            schema=TenderPositionsResponse,
            operation="extract_tender_products",
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
        started = time.monotonic()
        try:
            http_response = httpx.post(
                f"{self.settings.llm_base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": ocr_models[0],
                    "max_output_tokens": self.settings.llm_max_output_tokens,
                    "instructions": (
                        "Ты OCR-модуль. Верни только распознанный текст документа без markdown. "
                        "Сохраняй таблицы, числа и структуру строк."
                    ),
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_file",
                                    "filename": path.name,
                                    "file_data": f"data:application/pdf;base64,{data}",
                                    "detail": self.settings.ocr_pdf_detail,
                                },
                                {
                                    "type": "input_text",
                                    "text": (
                                        "Распознай весь текст PDF, сохраняя таблицы, цены, "
                                        "количества и единицы измерения."
                                    ),
                                },
                            ],
                        },
                    ],
                },
                timeout=self.settings.llm_timeout_seconds,
            )
            http_response.raise_for_status()
            response = http_response.json()
            if not isinstance(response, dict):
                raise ValueError("OpenAI Responses API returned a non-object response")
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
        return self._responses_output_text(response)
