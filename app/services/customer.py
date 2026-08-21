from __future__ import annotations

import re
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.models import NormalizedJob, ParsedDocument
from app.services.llm import LlmClient


class ActualCustomerResponse(BaseModel):
    selected: bool = False
    source: str = "not_found"
    selectedRole: str = "fallback"
    actualCustomerName: str | None = None
    actualCustomerInn: str | None = None
    actualCustomerKpp: str | None = None
    replaceInputInn: bool = False
    replaceInputKpp: bool = False
    confidence: Literal["low", "medium", "high"] = "low"
    evidence: str = ""
    warnings: list[str] = Field(default_factory=list)


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _valid_inn(value: Any) -> str | None:
    digits = _digits(value)
    return digits if len(digits) in {10, 12} else None


def _valid_kpp(value: Any) -> str | None:
    digits = _digits(value)
    return digits if len(digits) == 9 else None


def extract_candidates(documents: list[ParsedDocument], page_text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    sources = [(document.fileName, document.text, "document") for document in documents]
    sources.append(("Страница/Seldon", page_text, "tender_page_or_seldon"))
    role_pattern = re.compile(r"грузополучател|получател|филиал|обособлен|заказчик|покупател|сторона\s*1", re.I)
    inn_pattern = re.compile(r"(?:ИНН|И\s*Н\s*Н)\s*[:№#\-–—]?\s*((?:\d[\s-]*){10,12})", re.I)
    kpp_pattern = re.compile(r"(?:КПП|К\s*П\s*П)\s*[:№#\-–—]?\s*((?:\d[\s-]*){9})", re.I)
    for file_name, text, source_type in sources:
        for match in role_pattern.finditer(text or ""):
            fragment = (text or "")[max(0, match.start() - 900) : match.end() + 1900]
            inns = list(dict.fromkeys(filter(None, (_valid_inn(item) for item in inn_pattern.findall(fragment)))))
            kpps = list(dict.fromkeys(filter(None, (_valid_kpp(item) for item in kpp_pattern.findall(fragment)))))
            role_text = match.group(0).lower()
            role = "Грузополучатель" if "груз" in role_text else "Получатель" if "получ" in role_text else "Филиал" if "филиал" in role_text or "обособ" in role_text else "Заказчик"
            score = 1000 if role == "Грузополучатель" else 850 if role == "Получатель" else 800 if role == "Филиал" else 700
            if re.search(r"поставщик|исполнитель|подрядчик|банк|уфк|казначейств|грузоотправител", fragment, re.I):
                score -= 800
            candidates.append(
                {
                    "sourceType": source_type,
                    "fileName": file_name,
                    "role": role,
                    "inns": inns,
                    "kpps": kpps,
                    "score": score,
                    "evidence": re.sub(r"\s+", " ", fragment).strip()[:1800],
                }
            )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[:35]


def resolve_actual_customer(
    llm: LlmClient, job: NormalizedJob, fields: dict[str, Any], meta: dict[str, Any],
    documents: list[ParsedDocument], page_text: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str], dict[str, Any]]:
    candidates = extract_candidates(documents, page_text)
    fallback = {
        "name": fields.get("counterpartyName"),
        "inn": _valid_inn(fields.get("counterpartyInn")),
        "kpp": _valid_kpp(fields.get("counterpartyKpp")),
    }
    prompt = f"""
Определи фактического заказчика/получателя поставки. Только JSON.
Приоритет: грузополучатель, получатель, филиал, заказчик, покупатель.
У филиала ИНН может совпадать с головной компанией, а КПП отличаться.
Не выбирай поставщика, исполнителя, банк, УФК, казначейство или грузоотправителя.
Не выдумывай ИНН/КПП. При отсутствии document evidence используй Seldon/Daily fallback.
Fallback: {fallback}
Candidates: {candidates}
""".strip()
    warnings: list[str] = []
    resolution = llm.json_call(
        system="Выбери фактического заказчика/получателя. Только JSON.",
        prompt=prompt,
        schema=ActualCustomerResponse,
        operation="resolve_actual_customer",
    )
    updated_fields, updated_meta = dict(fields), dict(meta)
    name = str(resolution.actualCustomerName or "").strip()
    inn = _valid_inn(resolution.actualCustomerInn)
    kpp = _valid_kpp(resolution.actualCustomerKpp)
    source = f"Resolve Actual Customer: {resolution.source} / {resolution.selectedRole}"
    evidence = resolution.evidence[:900]
    if name:
        updated_fields["counterpartyName"] = name
        updated_meta["counterpartyName"] = {"source": source, "confidence": resolution.confidence, "evidence": evidence}
    if inn and (resolution.replaceInputInn or not fallback["inn"] or inn != fallback["inn"]):
        updated_fields["counterpartyInn"] = inn
        updated_meta["counterpartyInn"] = {"source": source, "confidence": resolution.confidence, "evidence": evidence}
    if kpp and (resolution.replaceInputKpp or not fallback["kpp"] or kpp != fallback["kpp"]):
        updated_fields["counterpartyKpp"] = kpp
        updated_meta["counterpartyKpp"] = {"source": source, "confidence": resolution.confidence, "evidence": evidence}
    updated_fields["counterparty"] = updated_fields.get("counterpartyName")
    updated_fields["inn"] = updated_fields.get("counterpartyInn")
    updated_fields["kpp"] = updated_fields.get("counterpartyKpp")
    warnings.extend(resolution.warnings)
    debug = {
        "inputBefore": fallback,
        "selected": resolution.model_dump(),
        "candidatesCount": len(candidates),
        "candidates": candidates,
    }
    return updated_fields, updated_meta, warnings, debug


class IProClient:
    def __init__(self, settings: Settings) -> None:
        headers = {"Accept": "application/json"}
        if settings.ipro_token:
            headers["Authorization"] = f"Bearer {settings.ipro_token.get_secret_value()}"
        self.client = httpx.Client(
            base_url=settings.ipro_base_url.rstrip("/") + "/",
            timeout=settings.http_read_timeout_seconds,
            headers=headers,
        )

    def close(self) -> None:
        self.client.close()

    def lookup(
        self, fields: dict[str, Any], meta: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
        updated_fields, updated_meta = dict(fields), dict(meta)
        warnings: list[str] = []
        inn = _valid_inn(fields.get("counterpartyInn") or fields.get("inn"))
        kpp = _valid_kpp(fields.get("counterpartyKpp") or fields.get("kpp"))
        if not inn:
            reason = "Для проверки IPro отсутствует корректный ИНН контрагента."
            return updated_fields, updated_meta, {"status": "not_found", "matchType": "none", "reason": reason, "inn": None, "kpp": kpp}, [reason]
        try:
            response = self.client.get("orgByBir", params={"inn": inn})
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            reason = f"IPro API не выполнил проверку по ИНН {inn}: {exc}"
            return updated_fields, updated_meta, {"status": "lookup_error", "reason": reason, "inn": inn, "kpp": kpp}, [reason]
        if isinstance(data, dict) and isinstance(data.get("body"), dict):
            data = data["body"]
        api_code_raw = None
        if isinstance(data, dict):
            status_object = data.get("status") if isinstance(data.get("status"), dict) else {}
            api_code_raw = status_object.get("code") or data.get("code") or data.get("statusCode")
        try:
            api_code = int(api_code_raw) if api_code_raw not in (None, "") else None
        except (TypeError, ValueError):
            api_code = None
        if api_code is not None and api_code != 200:
            message = (
                (data.get("status") or {}).get("message")
                or (data.get("status") or {}).get("descr")
                or data.get("message")
                or "нет описания"
            )
            reason = f"IPro API не выполнил проверку по ИНН {inn}. Код: {api_code}. Сообщение: {message}."
            return updated_fields, updated_meta, {"status": "lookup_error", "reason": reason, "inn": inn, "kpp": kpp}, [reason]
        rows: Any = []
        if isinstance(data, dict):
            rows = (
                ((data.get("data") or {}).get("rows") if isinstance(data.get("data"), dict) else None)
                or (((data.get("result") or {}).get("data") or {}).get("rows") if isinstance((data.get("result") or {}).get("data"), dict) else None)
                or ((data.get("result") or {}).get("rows") if isinstance(data.get("result"), dict) else None)
                or data.get("rows")
                or []
            )
            if not rows:
                one = (
                    ((data.get("data") or {}).get("row") if isinstance(data.get("data"), dict) else None)
                    or ((data.get("result") or {}).get("row") if isinstance(data.get("result"), dict) else None)
                    or data.get("row")
                )
                rows = [one] if isinstance(one, dict) else []
        if not isinstance(rows, list):
            rows = []

        def normalized(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "inn": _valid_inn(row.get("innOrg") or row.get("inn")),
                "kpp": _valid_kpp(row.get("kppOrg") or row.get("kpp")),
                "fullName": row.get("fullNameOrg") or row.get("fullName") or row.get("name"),
                "shortName": row.get("shortNameOrg") or row.get("shortName"),
                "raw": row,
            }

        candidates = [normalized(row) for row in rows if isinstance(row, dict)]
        by_inn = [row for row in candidates if row["inn"] == inn]
        # Business approval is based on INN only. Prefer the row with the same
        # KPP for display data when it exists, but a KPP mismatch never sends the
        # tender to counterparty review.
        match = next((row for row in by_inn if kpp and row["kpp"] == kpp), None)
        if match is None and by_inn:
            match = by_inn[0]
        match_type = "inn" if match else None
        if not match:
            reason = f"Организация с ИНН {inn} в IPro не найдена."
            match_kind = "none"
            lookup = {"status": "not_found", "matchType": match_kind, "reason": reason, "inn": inn, "kpp": kpp, "byInnCount": len(by_inn)}
            return updated_fields, updated_meta, lookup, [reason]
        name = match["fullName"] or match["shortName"] or fields.get("counterpartyName")
        if name:
            updated_fields["counterpartyName"] = name
            updated_fields["counterparty"] = name
            updated_fields["counterpartyFullName"] = name
        updated_fields["counterpartyInn"] = match["inn"] or inn
        updated_fields["inn"] = updated_fields["counterpartyInn"]
        if match["kpp"] and not kpp:
            updated_fields["counterpartyKpp"] = match["kpp"]
            updated_fields["kpp"] = match["kpp"]
        evidence = f"IPro API: найдено совпадение по ИНН {inn}; КПП в проверке не участвует."
        for key in ("counterpartyName", "counterpartyInn", "counterpartyKpp"):
            if updated_fields.get(key):
                updated_meta[key] = {"source": "IPro API /orgByBir", "confidence": "high", "evidence": evidence}
        updated_meta["counterparty"] = updated_meta.get("counterpartyName")
        updated_meta["counterpartyFullName"] = updated_meta.get("counterpartyName")
        lookup = {
            "status": "matched", "source": "ipro_api", "matchType": match_type,
            "reason": "", "requestedInn": inn, "requestedKpp": kpp,
            "inn": match["inn"] or inn, "kpp": match["kpp"] or kpp,
            "innOrg": match["inn"], "kppOrg": match["kpp"],
            "fullNameOrg": match["fullName"], "shortNameOrg": match["shortName"],
            "apiRowsCount": len(candidates), "byInnCount": len(by_inn),
        }
        return updated_fields, updated_meta, lookup, warnings
