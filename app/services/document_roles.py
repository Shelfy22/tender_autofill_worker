from __future__ import annotations

import re
import unicodedata
from typing import Any


TECHNICAL_ROLE = "technical_specification"
PRICE_ROLE = "price_justification"
CONTRACT_ROLE = "contract"
COMPOSITE_ROLE = "composite"
OTHER_ROLE = "other"


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


_PRICE_PATTERN = re.compile(
    r"\b(?:обосновани[ея]|расчет|расчетная)\b.{0,80}\b(?:нмц[кд]?|цен[а-я]*)\b|"
    r"\bсведени[ея]\b.{0,80}\bначальн[а-я]*\b.{0,40}\bмаксимальн[а-я]*\b"
    r".{0,80}\bцен[а-я]*\b.{0,80}\b(?:единиц[а-я]*|продукц[а-я]*)\b|"
    r"\bначальн[а-я]*\b.{0,30}\bмаксимальн[а-я]*\b.{0,50}\bцен[а-я]*\b"
    r".{0,60}\bкажд[а-я]*\s+единиц[а-я]*\b|"
    r"\bнмц[кд]?\b"
)
_TECHNICAL_PATTERN = re.compile(
    r"\bтехническ[а-я]*\s+(?:задани[ея]|требовани[ея]|спецификаци[яи])\b|"
    r"\bописани[ея]\s+объект[а-я]*\s+закупк[а-я]*\b|"
    r"\bспецификаци[яи]\b.{0,80}\bсогласно\s+(?:техническ[а-я]*\s+задани[юя]|тз)\b|"
    r"(?:^|\s)тз(?:\s|$)"
)
_CONTRACT_PATTERN = re.compile(r"\bпроект[а-я]*\s+договор[а-я]*\b")


def detect_section_role(value: Any) -> str:
    text = _normalized(value)
    if not text:
        return OTHER_ROLE
    if _PRICE_PATTERN.search(text):
        return PRICE_ROLE
    if _TECHNICAL_PATTERN.search(text):
        return TECHNICAL_ROLE
    if _CONTRACT_PATTERN.search(text):
        return CONTRACT_ROLE
    return OTHER_ROLE


def classify_document_role(*values: Any) -> str:
    roles: set[str] = set()
    for value in values:
        text = str(value or "")
        direct = detect_section_role(text)
        if direct != OTHER_ROLE:
            roles.add(direct)
        for line in text.splitlines():
            role = detect_section_role(line)
            if role != OTHER_ROLE:
                roles.add(role)
    if len(roles) > 1:
        return COMPOSITE_ROLE
    return next(iter(roles), OTHER_ROLE)


def source_role_priority(role: Any) -> int:
    normalized = _normalized(role)
    return {
        PRICE_ROLE: 0,
        "price justification": 0,
        TECHNICAL_ROLE: 1,
        "technical specification": 1,
        "technical": 1,
        "specification": 1,
        COMPOSITE_ROLE: 2,
        OTHER_ROLE: 3,
        "document": 3,
        CONTRACT_ROLE: 4,
    }.get(normalized, 3)
