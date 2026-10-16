"""Prepare narrow, evidence-first context for tender commercial conditions.

The expressions in procurement documents vary too much to derive a card value
with a regular expression safely.  This module deliberately only retrieves
small source fragments.  An LLM verifies their meaning and extracts values.
"""

from __future__ import annotations

import re
from typing import Iterable

from app.models import ParsedDocument


_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "nationalRegime",
        re.compile(r"национальн\w*\s+режим|постановлен\w*\s*№?\s*1875|пп\s*№?\s*1875", re.I),
    ),
    (
        "paymentDelayDays",
        re.compile(r"оплат\w*|плат[её]ж\w*|расч[её]т\w*|перечислен\w*", re.I),
    ),
    (
        "delivery",
        re.compile(r"срок\w*\s+поставк\w*|поставк\w*|парт\w*|заявк\w*|разнаряд\w*", re.I),
    ),
    (
        "warrantyMonths",
        re.compile(r"гарант\w*", re.I),
    ),
    (
        "resultDate",
        re.compile(r"подведен\w*\s+итог\w*|итог\w*\s+закупк\w*|оценк\w*\s+и\s+сопоставлен", re.I),
    ),
)


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value or " ").strip()


def _fragment(text: str, start: int, end: int, radius: int = 700) -> str:
    """Return a bounded paragraph-like window without cutting the key phrase."""
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    before = text.rfind("\n\n", left, start)
    after = text.find("\n\n", end, right)
    if before >= 0:
        left = before + 2
    if after >= 0:
        right = after
    return _compact(text[left:right])[:1_800]


def _source_blocks(page_text: str, documents: Iterable[ParsedDocument]) -> Iterable[tuple[str, str]]:
    if _compact(page_text):
        yield "Страница тендера", str(page_text)
    for document in documents:
        if _compact(document.text):
            yield document.fileName or document.originalFileName or "Документ без имени", document.text


def build_commercial_conditions_context(
    page_text: str,
    documents: Iterable[ParsedDocument],
    *,
    max_fragments: int = 48,
    max_chars: int = 48_000,
) -> str:
    """Return relevant evidence, not guessed field values.

    Each fragment has its source and target fields.  It is intentionally
    conservative: a date or number is never emitted unless its surrounding
    text mentions a commercial-condition anchor.
    """
    fragments: list[str] = []
    seen: set[tuple[str, str]] = set()
    for source, raw_text in _source_blocks(page_text, documents):
        text = str(raw_text or "")
        for label, pattern in _FIELD_PATTERNS:
            for match in pattern.finditer(text):
                value = _fragment(text, match.start(), match.end())
                fingerprint = (source.casefold(), value.casefold())
                if not value or fingerprint in seen:
                    continue
                seen.add(fingerprint)
                fragments.append(f"[source: {source}; targets: {label}]\n{value}")
                if len(fragments) >= max_fragments:
                    break
            if len(fragments) >= max_fragments:
                break
        if len(fragments) >= max_fragments:
            break

    result: list[str] = []
    used = 0
    for fragment in fragments:
        if used + len(fragment) + 2 > max_chars:
            break
        result.append(fragment)
        used += len(fragment) + 2
    return "\n\n".join(result)
