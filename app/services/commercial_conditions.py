"""Prepare narrow, evidence-first context for tender commercial conditions.

The expressions in procurement documents vary too much to derive a card value
with a regular expression safely.  This module deliberately only retrieves
small source fragments.  An LLM verifies their meaning and extracts values.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from app.models import ParsedDocument


@dataclass(frozen=True)
class _Anchor:
    """A retrieval expression and its evidence quality.

    ``strong`` anchors explicitly name the field.  ``supporting`` anchors
    cover common procurement wording but are only considered after the strong
    matches, so a generic word cannot crowd the prompt out.
    """

    pattern: re.Pattern[str]
    strong: bool = True


_FIELD_PATTERNS: dict[str, tuple[_Anchor, ...]] = {
    "nationalRegime": (
        _Anchor(re.compile(r"национальн\w*\s+режим", re.I)),
        _Anchor(re.compile(r"(?:постановлен\w*|пп)\s*(?:правительств\w*\s*(?:рф|российской федерации)\s*)?№?\s*1875", re.I)),
        _Anchor(re.compile(r"минимальн\w*\s+обязательн\w*\s+дол", re.I)),
        _Anchor(re.compile(r"(?:запрет|ограничени\w*|преимуществ\w*)\s+(?:закуп\w*|установлен\w*|применя\w*)", re.I), False),
    ),
    "paymentDelayDays": (
        _Anchor(re.compile(r"отсрочк\w*\s+(?:платеж\w*|оплат\w*)", re.I)),
        _Anchor(re.compile(r"срок\w*\s+оплат\w*", re.I)),
        _Anchor(re.compile(r"(?:порядок|услови\w*)\s+(?:оплат\w*|расч[её]т\w*)", re.I)),
        _Anchor(re.compile(r"(?:расч[её]т\w*|оплат\w*)\s+производ\w*", re.I)),
        _Anchor(re.compile(r"перечислен\w*\s+денежн\w*\s+средств", re.I), False),
        _Anchor(re.compile(r"оплат\w*.{0,120}?производ\w*", re.I), False),
        _Anchor(re.compile(r"поставленн\w*\s+(?:товар|продукци\w*)\s+оплач", re.I), False),
    ),
    "delivery": (
        _Anchor(re.compile(r"срок\w*\s+поставк\w*", re.I)),
        _Anchor(re.compile(r"(?:услови\w*|порядок|график|дат[аы])\s+поставк\w*", re.I)),
        _Anchor(re.compile(r"поставк\w*\s+(?:по\s+заявк\w*|партия\w*|единовременн\w*)", re.I)),
        _Anchor(re.compile(r"(?:кажд\w*\s+)?партия\w*\s+(?:товар\w*\s+)?(?:поставля\w*|в\s+течени\w*)", re.I), False),
        _Anchor(re.compile(r"заявк\w*\s+(?:заказчик\w*|покупател\w*)", re.I), False),
        _Anchor(re.compile(r"поставк\w*", re.I), False),
    ),
    "warrantyMonths": (
        _Anchor(re.compile(r"гарантийн\w*\s+срок", re.I)),
        _Anchor(re.compile(r"срок\w*\s+гаранти\w*", re.I)),
        _Anchor(re.compile(r"гарантийн\w*\s+обязательств\w*", re.I)),
        _Anchor(re.compile(r"гаранти\w*\s+на\s+(?:товар|оборудовани\w*|продукци\w*)", re.I)),
        _Anchor(re.compile(r"послегарантийн\w*\s+обслуживан", re.I), False),
        _Anchor(re.compile(r"гаранти\w*", re.I), False),
    ),
    "resultDate": (
        _Anchor(re.compile(r"дат[аы]\s+подведен\w*\s+итог\w*", re.I)),
        _Anchor(re.compile(r"подведен\w*\s+итог\w*\s+закупк\w*", re.I)),
        _Anchor(re.compile(r"(?:рассмотрени\w*\s+заявок|оценк\w*\s+и\s+сопоставлен\w*\s+заявок).*?(?:итог|дат)", re.I)),
        _Anchor(re.compile(r"итог\w*\s+закупк\w*", re.I), False),
    ),
}


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
    max_fragments_per_field: int = 10,
) -> str:
    """Return relevant evidence, not guessed field values.

    Each fragment has its source and target fields.  It is intentionally
    conservative: a date or number is never emitted unless its surrounding
    text mentions a commercial-condition anchor.
    """
    # Gather first and select in rounds below.  Selecting while traversing the
    # files used to let frequent words such as "поставка" fill the entire
    # global limit before the payment or national-regime evidence was reached.
    candidates: dict[str, dict[bool, list[tuple[str, str]]]] = defaultdict(
        lambda: {True: [], False: []}
    )
    seen: set[tuple[str, str, str]] = set()
    # A repeated table in one file must not hide evidence in the other tender
    # documents.  We retain several alternatives per source, but no more.
    candidate_limit = max(max_fragments_per_field * 5, 20)
    source_candidate_counts: dict[tuple[str, str], int] = defaultdict(int)
    for source, raw_text in _source_blocks(page_text, documents):
        text = str(raw_text or "")
        for label, anchors in _FIELD_PATTERNS.items():
            for anchor in anchors:
                for match in anchor.pattern.finditer(text):
                    source_key = (label, source.casefold())
                    if source_candidate_counts[source_key] >= 3:
                        break
                    value = _fragment(
                        text,
                        match.start(),
                        match.end(),
                        radius=750 if anchor.strong else 500,
                    )
                    fingerprint = (label, source.casefold(), value.casefold())
                    if not value or fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    candidates[label][anchor.strong].append((source, value))
                    source_candidate_counts[source_key] += 1
                    if sum(len(group) for group in candidates[label].values()) >= candidate_limit:
                        break
                if sum(len(group) for group in candidates[label].values()) >= candidate_limit:
                    break

    fragments: list[str] = []
    selected_per_field: dict[str, int] = defaultdict(int)
    # Strong evidence always gets first choice; within a quality tier the
    # round-robin preserves room for every commercial field.
    for strong in (True, False):
        offsets: dict[str, int] = defaultdict(int)
        while len(fragments) < max_fragments:
            added = False
            for label in _FIELD_PATTERNS:
                if selected_per_field[label] >= max_fragments_per_field:
                    continue
                field_candidates = candidates[label][strong]
                offset = offsets[label]
                if offset >= len(field_candidates):
                    continue
                source, value = field_candidates[offset]
                offsets[label] += 1
                quality = "strong" if strong else "supporting"
                fragments.append(
                    f"[source: {source}; targets: {label}; anchor: {quality}]\n{value}"
                )
                selected_per_field[label] += 1
                added = True
                if len(fragments) >= max_fragments:
                    break
            if not added:
                break

    result: list[str] = []
    used = 0
    for fragment in fragments:
        if used + len(fragment) + 2 > max_chars:
            break
        result.append(fragment)
        used += len(fragment) + 2
    return "\n\n".join(result)
