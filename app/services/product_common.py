from __future__ import annotations
import json
import re
import unicodedata
from typing import Any
from app.models import ProductSourceReference, TenderPosition
from app.services.document_roles import OTHER_ROLE, TECHNICAL_ROLE, classify_document_role

_ONLY_ROW_NUMBER_PATTERN = re.compile(
    r"^\s*[+-]?\d+(?:[.,]\d+)?\s*[.)-]?\s*$"
)


_ONLY_CLASSIFIER_CODE_PATTERN = re.compile(
    r"^\s*\d{2}(?:[.\s-]\d{1,3}){2,}(?:\s*[.)-]?)?\s*$"
)


_ONLY_AUXILIARY_CODE_PATTERN = re.compile(
    r"^\s*(?:ол|ol)\s*[-–—]?\s*\d{1,4}\s*$",
    re.IGNORECASE,
)


_EXAMPLE_POSITION_PATTERN = re.compile(
    r"^\s*(?:пример(?:\s+заполнения)?|example)\s*[.:;–—-]?\s*$",
    re.IGNORECASE,
)


_SERVICE_POSITION_PATTERN = re.compile(
    r"^\s*(?:национальн[а-яё]*\s+режим|"
    r"(?:ограничени[ея]|запрет[а-яё]*)\s+(?:не\s+)?(?:установлен[а-яё]*|предоставля[а-яё]*)|"
    r"товар\s+(?:не\s+)?(?:отсутствует|включ[её]н)\s+в\s+реестр|"
    r"код\s+(?:окпд|окпд2|ктру|тн\s+вэд)|"
    r"единиц[аы]\s+измерени[яй]|количеств[оа]|итого|всего)\b",
    re.IGNORECASE,
)


_ADDRESS_OR_RECIPIENT_PATTERN = re.compile(
    r"(?:\b(?:место|адрес)\s+(?:поставки|доставки)\b|"
    r"\b(?:грузополучатель|получатель)\b|"
    r"\b\d{6}\b.{0,160}(?:\bг\.|\bгород\b|\bул\.|\bулица\b|\bд\.|\bдом\b)|"
    r"\b(?:филиал|предприятие)\b.{0,160}(?:\bг\.|\bгород\b|\bул\.|\bулица\b))",
    re.IGNORECASE,
)


_CLASSIFIER_SUFFIX_PATTERN = re.compile(
    r"\s*(?:код\s+)?(?:окпд2?|ктру|тн\s+вэд)\s*:?\s*\d{2}(?:[.\s-]\d{1,3}){2,}\s*$",
    re.IGNORECASE,
)


_EQUIVALENT_SUFFIX_PATTERN = re.compile(
    r"\s*[([]?\s*(?:или\s+)?(?:аналог|эквивалент)\s*[)\]]?\s*$",
    re.IGNORECASE,
)


_CONDITION_POSITION_PATTERN = re.compile(
    r"^\s*(?:"
    r"аналоги?\s+рассматрива(?:ются|ется)(?:\s*[.!;:]?\s*допуск(?:\s+по)?\s+[а-яё\s]+\s*[±+\-]?\s*\d+(?:[,.]\d+)?\s*%)?|"
    r"эквиваленты?\s+(?:допуска(?:ются|ется)|разрешены?)|"
    r"аналог\s+допуска(?:ется|ются)|без\s+аналогов|"
    r"допуск(?:\s+по)?\s+(?:габарит[а-яё]*|толщин[а-яё]*|размер[а-яё]*)\s*[±+\-]?\s*\d+(?:[,.]\d+)?\s*%|"
    r"согласно\s+(?:техническому\s+задани[юя]|тз)|"
    r"(?:не\s+менее|не\s+более|не\s+хуже|до|от)\s+\d+(?:[,.]\d+)?\s*(?:месяц[а-яё]*|мес\.?|дн(?:ей|я)?|сут(?:ок|ки)?|лет|год[а-яё]*|%)"
    r")\s*[.!;:]*\s*$",
    re.IGNORECASE,
)


_APPLICATION_REFERENCE_POSITION_PATTERN = re.compile(
    r"^\s*\*?\s*в\s+соответствии\s+с\s+поданной\s+заявкой(?:\s+участника)?"
    r"(?:\s*(?:и|,)?\s*приложени[ея].*)?\s*\*?\s*$",
    re.IGNORECASE,
)


_PRODUCT_COPY_TAIL_PATTERN = re.compile(
    r"(?:\s*[;,.]*\s*\(?\s*(?:или\s+)?(?:эквивалент|аналог)\s*\)?|"
    r"\s*[;,.]*\s*\(?\s*\*?\s*допустимо\s+изменение\s+наименования\s+товара.*?\)?)\s*$",
    re.IGNORECASE,
)


_PRODUCT_DESCRIPTION_SEPARATOR_PATTERN = re.compile(
    r"^(.{3,300}?):\s*((?:назначение|технические\s+характеристики|"
    r"характеристики|описание)\s*:?.*)$",
    re.IGNORECASE | re.DOTALL,
)


_TENDER_LEVEL_PRICE_PATTERN = re.compile(
    r"\b(?:начальн[а-яё]*\s+(?:максимальн[а-яё]*\s+)?цен[аы]|нмцк?|нмц)\b",
    re.IGNORECASE,
)


_POSITION_PRICE_PATTERN = re.compile(
    r"\b(?:цен[аы]\s+(?:за\s+)?(?:единиц[уы]|1\s*(?:шт|ед))|"
    r"стоимост[ьи]\s+(?:единиц[уы]|позиц[а-яё]*|строк[а-яё]*)|"
    r"сумм[аы]\s+(?:строк[а-яё]*|позиц[а-яё]*))\b",
    re.IGNORECASE,
)


_DELIVERY_SCHEDULE_HEADING_PATTERN = re.compile(
    r"(?:\u0433\u0440\u0430\u0444\u0438\u043a|\u043f\u043b\u0430\u043d)\s+(?:\u043f\u043e\u0441\u0442\u0430\u0432\u043a|\u043e\u0442\u0433\u0440\u0443\u0437\u043a|\u0434\u043e\u0441\u0442\u0430\u0432\u043a)",
    re.IGNORECASE,
)


_DELIVERY_SCHEDULE_COLUMN_PATTERN = re.compile(
    r"(?:\u0441\u0440\u043e\u043a|\u0434\u0430\u0442\u0430|\u043f\u0435\u0440\u0438\u043e\u0434|\u043c\u0435\u0441\u044f\u0446)\s+(?:\u043f\u043e\u0441\u0442\u0430\u0432\u043a|\u043e\u0442\u0433\u0440\u0443\u0437\u043a|\u0434\u043e\u0441\u0442\u0430\u0432\u043a)",
    re.IGNORECASE,
)


UNITS = r"штука|штук|шт\.?|комплект|компл\.?|набор|ед\.?|метр|м|кг|л|уп\.?|упак\.?"


_TABLE_IDENTITY_HEADER_PATTERN = re.compile(
    r"(?:\bмарка\b|\bмодель\b|\bартикул\b|\bобозначени|\bтип\b)",
    re.IGNORECASE,
)


_TABLE_IDENTITY_EXCLUDED_HEADER_PATTERN = re.compile(
    r"(?:предложени.*участник|страна|техническ.*регламент|стандарт)",
    re.IGNORECASE,
)


_STRUCTURED_TABLE_MARKER_PATTERN = re.compile(
    r"^\s*\u0422\u0430\u0431\u043b\u0438\u0446\u0430\s+(Word|PDF|RTF)\s+(\d+)\s*$",
    re.IGNORECASE,
)


_WORD_TABLE_ROW_PATTERN = re.compile(
    r"^\s*\u0421\u0442\u0440\u043e\u043a\u0430\s+(\d+)\s*:\s*(.+)$",
    re.IGNORECASE,
)


_MODEL_LOOKALIKE_TRANSLATION = str.maketrans(
    {
        "\u0410": "A", "\u0412": "B", "\u0421": "C", "\u0415": "E", "\u041d": "H", "\u041a": "K",
        "\u041c": "M", "\u041e": "O", "\u0420": "P", "\u0422": "T", "\u0425": "X", "\u0423": "Y",
        "\u0430": "a", "\u0432": "b", "\u0441": "c", "\u0435": "e", "\u043d": "h", "\u043a": "k",
        "\u043c": "m", "\u043e": "o", "\u0440": "p", "\u0442": "t", "\u0445": "x", "\u0443": "y",
    }
)


_MODEL_GENERIC_WORDS = (
    "\u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439",
    "\u0432\u044b\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u044c",
    "\u043f\u0443\u0441\u043a\u0430\u0442\u0435\u043b\u044c",
    "\u043c\u0430\u0433\u043d\u0438\u0442\u043d\u044b\u0439",
)


_MODEL_ROW_SUFFIX_PATTERN = re.compile("\u0441\u0442\u0440\u043e\u043a\u0430\\s*\\d+", re.IGNORECASE)


_GENERIC_NAME_WORDS = {
    "или", "эквивалент", "поставка", "товар", "товары", "для", "с",
    "и", "в", "на", "по", "из", "со",
}


_POWER_TRANSFORMER_SERIES_PATTERN = re.compile(r"\bтмгф?\b", re.IGNORECASE)


_POWER_TRANSFORMER_CAPACITY_PATTERN = re.compile(
    r"(?:\bтмгф?\s*[- ]*|\b)(\d{2,5})(?=\s*(?:ква|кв\s*а|/))",
    re.IGNORECASE,
)


_OKPD_PLACEHOLDER_PRODUCT_PATTERN = re.compile(
    r"^\s*\S+(?:\s+\S+){0,4}\s*\(\s*\u043e\u043a\u043f\u0434\s+\d{2}(?:\.\d{1,3}){1,4}\s*\)\s*$",
    re.IGNORECASE,
)


_SURVEY_SHEET_FILE_PATTERN = re.compile(
    r"(?:\b\u043e\u043f\u0440\u043e\u0441\u043d\w*\s+\u043b\u0438\u0441\u0442\b|"
    r"(?:^|\W)\u043e\u043b\s*(?:\u2116|n|#)?\s*\d+)",
    re.IGNORECASE,
)


_TENDER_SUBJECT_PRODUCT_PATTERN = re.compile(
    r"^\s*(?:\u043a\u0442\u043f|\u0441\u0442\u043f|\u043c\u0442\u043f)"
    r"(?:\s*,\s*(?:\u043a\u0442\u043f|\u0441\u0442\u043f|\u043c\u0442\u043f)){1,}"
    r"\s+\u0431\u0435\u0437\s+\u0441\u0438\u043b\u043e\u0432\w*\s+\u0442\u0440\u0430\u043d\u0441\u0444\u043e\u0440\u043c\u0430\u0442\u043e\u0440\w*\s*$",
    re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("undefined", " ")).strip()

def _missing(value: Any) -> bool:
    return value is None or value == ""

def _normalize_header(value: Any) -> str:
    text = _clean(value).lower().replace("ё", "е")
    text = re.sub(r"\bколи-\s*чество\b", "количество", text, flags=re.IGNORECASE)
    text = re.sub(r"\bдиа-\s*метр\b", "диаметр", text, flags=re.IGNORECASE)
    return re.sub(r"[^a-zа-я0-9№]+", " ", text).strip()

def _parse_structured_cells(value: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for raw_part in value.split("|"):
        match = re.match(r"^\s*([A-Z]{1,3}):\s*(.*?)\s*$", raw_part)
        if match and match.group(2):
            cells[match.group(1)] = match.group(2)
    return cells

def _word_table_key(value: Any) -> str:
    return re.sub(
        r"[^a-z\u0410-\u044f0-9]+",
        " ",
        _clean(value).casefold().replace("\u0451", "\u0435"),
    ).strip()

def _excel_column_number(value: str) -> int:
    number = 0
    for character in value.upper():
        if not "A" <= character <= "Z":
            return 0
        number = number * 26 + ord(character) - ord("A") + 1
    return number

def _position_name_key(position: TenderPosition) -> str:
    value = _clean(position.product).lower().replace("ё", "е")
    description_match = _PRODUCT_DESCRIPTION_SEPARATOR_PATTERN.match(value)
    if description_match:
        value = _clean(description_match.group(1))
    value = re.sub(
        r"\s*[([]?\s*(?:или\s+)?(?:аналог|эквивалент)\s*[)\]]?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()

def _position_source_role(position: TenderPosition) -> str:
    reference = position.sourceReference
    if reference is None:
        return OTHER_ROLE
    if reference.sectionRole and reference.sectionRole != OTHER_ROLE:
        return reference.sectionRole
    return classify_document_role(reference.fileName, reference.sheet, reference.table)

def _product_name_stems(position: TenderPosition) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]+", _clean(position.product).casefold())
    return {
        word[:5]
        for word in words
        if len(word) >= 3 and word not in _GENERIC_NAME_WORDS and not word.isdigit()
    }

def _source_reference_key(reference: ProductSourceReference | None) -> tuple[str, str, int | None, str]:
    if reference is None:
        return ("", "", None, "")
    return (
        _word_table_key(reference.fileName),
        _word_table_key(reference.sheet or reference.table),
        reference.row,
        _word_table_key(reference.productColumn),
    )

def _all_source_references(position: TenderPosition) -> list[ProductSourceReference]:
    # Keep the canonical row first. Source references are bounded, therefore
    # appending it after a long list of allocation rows could hide the primary
    # TЗ/specification evidence.
    references = (
        [position.sourceReference] if position.sourceReference is not None else []
    ) + list(position.sourceReferences)
    result: list[ProductSourceReference] = []
    seen: set[tuple[str, str, int | None, str]] = set()
    for reference in references:
        key = _source_reference_key(reference)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(reference)
    return result

def _product_identity_specificity(position: TenderPosition) -> tuple[int, int, int, int]:
    """Prefer a concrete model/article over a generic table label."""
    title = _clean(position.product)
    model_or_article = int(bool(_clean(position.model) or _clean(position.article)))
    model_tokens = len(re.findall(r"(?=.*[a-zа-яё])(?=.*\d)[a-zа-яё0-9./_-]{3,}", title.casefold()))
    return (
        model_or_article,
        model_tokens,
        len(_product_name_stems(position)),
        len(title),
    )

def _preferred_product_identity(
    canonical: TenderPosition,
    duplicate: TenderPosition,
) -> TenderPosition:
    # A document LLM often repeats a structured row as a verbose phrase. It
    # must not replace the table's product identity merely because it is longer.
    if duplicate.source == "llm" and canonical.source != "llm":
        return canonical
    if (
        _position_source_role(canonical) == TECHNICAL_ROLE
        and _position_source_role(duplicate) == "price_justification"
    ):
        return canonical
    # A price-list row is already a valid canonical name unless the other
    # structured table explicitly supplies a model or article.
    if (
        _position_source_role(canonical) == "price_justification"
        and not (_clean(duplicate.model) or _clean(duplicate.article))
    ):
        return canonical
    if _product_identity_specificity(duplicate) <= _product_identity_specificity(canonical):
        return canonical
    updates: dict[str, Any] = {"product": duplicate.product}
    for field in ("productQuery", "model", "article", "brand", "sourceReference", "sourceCells"):
        value = getattr(duplicate, field)
        if not _missing(value):
            updates[field] = value
    return canonical.model_copy(update=updates)

def _merge_replicated_position_details(
    canonical: TenderPosition,
    duplicate: TenderPosition,
) -> TenderPosition:
    canonical = _preferred_product_identity(canonical, duplicate)
    updates: dict[str, Any] = {}
    characteristics = list(canonical.characteristics)
    seen_characteristics = {
        (_word_table_key(item.name), _word_table_key(item.value))
        for item in characteristics
    }
    for characteristic in duplicate.characteristics:
        # NMC rows can add authoritative document prices, but must never
        # contaminate a technical product with market-price calculation fields.
        if _position_source_role(duplicate) == "price_justification":
            continue
        key = (
            _word_table_key(characteristic.name),
            _word_table_key(characteristic.value),
        )
        if key not in seen_characteristics:
            characteristics.append(characteristic)
            seen_characteristics.add(key)
    if characteristics != canonical.characteristics:
        updates["characteristics"] = characteristics

    duplicate_requirements = _clean(duplicate.requirements)
    canonical_requirements = _clean(canonical.requirements)
    if duplicate_requirements and duplicate_requirements not in canonical_requirements:
        updates["requirements"] = _clean(
            "; ".join(filter(None, (canonical_requirements, duplicate_requirements)))
        )

    duplicate_evidence = _clean(duplicate.evidence)
    canonical_evidence = _clean(canonical.evidence)
    if duplicate_evidence and duplicate_evidence not in canonical_evidence:
        updates["evidence"] = _clean(
            " | ".join(filter(None, (canonical_evidence, duplicate_evidence)))
        )[:1200]

    source_references = _all_source_references(canonical) + _all_source_references(duplicate)
    deduplicated_references: list[ProductSourceReference] = []
    seen_references: set[tuple[str, str, int | None, str]] = set()
    for reference in source_references:
        key = _source_reference_key(reference)
        if key in seen_references:
            continue
        seen_references.add(key)
        deduplicated_references.append(reference)
    if deduplicated_references != canonical.sourceReferences:
        # TenderPosition deliberately bounds this diagnostic list. A schedule
        # can contain hundreds of allocation rows, but the canonical source
        # and the first supporting rows are enough to investigate it.
        updates["sourceReferences"] = deduplicated_references[:50]

    # Price is authoritative in an NMC/price-justification table, regardless
    # of which source donated the most concrete product title.
    if _position_source_role(duplicate) == "price_justification":
        for field in (
            "documentUnitPriceRub",
            "documentLineTotalRub",
            "documentCurrency",
            "documentPriceEvidence",
            "documentPriceSource",
        ):
            value = getattr(duplicate, field)
            if not _missing(value):
                updates[field] = value
    return canonical.model_copy(update=updates) if updates else canonical
