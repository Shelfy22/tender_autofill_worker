from __future__ import annotations
import json
import re
import unicodedata
from typing import Any
from app.models import ProductReconciliationFact, ProductSourceReference, TenderPosition, TenderPositionsResponse
from app.services.document_roles import COMPOSITE_ROLE, OTHER_ROLE, PRICE_ROLE, TECHNICAL_ROLE, source_role_priority

from app.services.product_common import (
    UNITS,
    _ADDRESS_OR_RECIPIENT_PATTERN,
    _APPLICATION_REFERENCE_POSITION_PATTERN,
    _CONDITION_POSITION_PATTERN,
    _EXAMPLE_POSITION_PATTERN,
    _MODEL_GENERIC_WORDS,
    _MODEL_LOOKALIKE_TRANSLATION,
    _MODEL_ROW_SUFFIX_PATTERN,
    _OKPD_PLACEHOLDER_PRODUCT_PATTERN,
    _ONLY_AUXILIARY_CODE_PATTERN,
    _ONLY_CLASSIFIER_CODE_PATTERN,
    _ONLY_ROW_NUMBER_PATTERN,
    _POSITION_PRICE_PATTERN,
    _POWER_TRANSFORMER_CAPACITY_PATTERN,
    _POWER_TRANSFORMER_SERIES_PATTERN,
    _PRODUCT_COPY_TAIL_PATTERN,
    _PRODUCT_DESCRIPTION_SEPARATOR_PATTERN,
    _SERVICE_POSITION_PATTERN,
    _SURVEY_SHEET_FILE_PATTERN,
    _TENDER_LEVEL_PRICE_PATTERN,
    _TENDER_SUBJECT_PRODUCT_PATTERN,
    _all_source_references,
    _clean,
    _excel_column_number,
    _merge_replicated_position_details,
    _missing,
    _normalize_header,
    _parse_structured_cells,
    _position_name_key,
    _position_source_role,
    _product_name_stems,
    _source_reference_key,
    _word_table_key,
)

def _is_noise_position(position: TenderPosition) -> bool:
    product = _clean(position.product)
    return bool(
        not product
        or _ONLY_ROW_NUMBER_PATTERN.fullmatch(product)
        or _ONLY_CLASSIFIER_CODE_PATTERN.fullmatch(product)
        or _ONLY_AUXILIARY_CODE_PATTERN.fullmatch(product)
        or _EXAMPLE_POSITION_PATTERN.fullmatch(product)
        or _SERVICE_POSITION_PATTERN.search(product)
        or _CONDITION_POSITION_PATTERN.fullmatch(product)
        or _APPLICATION_REFERENCE_POSITION_PATTERN.fullmatch(product)
        or _ADDRESS_OR_RECIPIENT_PATTERN.search(product)
        or _is_tender_subject_or_survey_title(position)
    )

def _normalize_product_description(position: TenderPosition) -> TenderPosition:
    """Keep the product title as identity and move an inline specification to requirements."""
    product = _clean(position.product)
    match = _PRODUCT_DESCRIPTION_SEPARATOR_PATTERN.match(product)
    if not match:
        return position
    title = _clean(match.group(1))
    description = _clean(match.group(2))
    if not title or not description:
        return position
    requirements = _clean(" ".join(filter(None, (position.requirements, description))))
    query = _clean(position.productQuery)
    return position.model_copy(
        update={
            "product": title,
            "productQuery": title if not query or query == product else query,
            "requirements": requirements,
        }
    )

def _clear_tender_level_price(position: TenderPosition) -> TenderPosition:
    """Do not treat NMCK/initial tender price as a product-position price."""
    evidence = _clean(position.documentPriceEvidence or position.evidence)
    if (
        position.documentPriceSource is None
        and _TENDER_LEVEL_PRICE_PATTERN.search(evidence)
        and not _POSITION_PRICE_PATTERN.search(evidence)
    ):
        return position.model_copy(
            update={
                "documentUnitPriceRub": None,
                "documentLineTotalRub": None,
                "documentCurrency": None,
                "documentPriceEvidence": "",
            }
        )
    return position

def _plain_number(value: Any) -> float | None:
    text = str(value or "").strip().replace("\xa0", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:[,.]\d+)?", text):
        return None
    return float(text.replace(",", "."))

def _quantity_from_structured_evidence(position: TenderPosition) -> float | None:
    """Recover quantity from the declared spreadsheet quantity column when available."""
    reference = position.sourceReference
    quantity_column = _clean(reference.quantityColumn if reference else "").upper()

    # A price column can be immediately to the right of the unit column.  Do not
    # infer quantity from adjacency when the parser has already identified the
    # real quantity column (for example, G=unit, H=unit price, J=quantity).
    if quantity_column:
        direct_value = _plain_number(position.sourceCells.get(quantity_column, ""))
        if direct_value is not None:
            return direct_value
        for evidence in (position.evidence, position.documentPriceEvidence):
            for row in str(evidence or "").splitlines():
                row_match = re.search(r"(?:^|\b)РЎС‚СЂРѕРєР°\s+\d+\s*:\s*(.+)$", row, re.IGNORECASE)
                if not row_match:
                    continue
                direct_value = _plain_number(
                    _parse_structured_cells(row_match.group(1)).get(quantity_column, "")
                )
                if direct_value is not None:
                    return direct_value
        return None

    # Fallback for legacy evidence that has no column metadata.
    expected_unit = _clean(position.unit).casefold().replace("ё", "е")
    for evidence in (position.evidence, position.documentPriceEvidence):
        for row in str(evidence or "").splitlines():
            row_match = re.search(r"(?:^|\b)Строка\s+\d+\s*:\s*(.+)$", row, re.IGNORECASE)
            if not row_match:
                continue
            cells = _parse_structured_cells(row_match.group(1))
            ordered = sorted(cells.items(), key=lambda item: _excel_column_number(item[0]))
            for index, (_, cell_value) in enumerate(ordered):
                normalized_cell = _clean(cell_value).casefold().replace("ё", "е")
                is_unit = bool(re.fullmatch(UNITS, cell_value, re.IGNORECASE))
                if expected_unit and normalized_cell == expected_unit:
                    is_unit = True
                if not is_unit or index + 1 >= len(ordered):
                    continue
                current_column = _excel_column_number(ordered[index][0])
                next_column = _excel_column_number(ordered[index + 1][0])
                if next_column != current_column + 1:
                    continue
                quantity = _plain_number(ordered[index + 1][1])
                if quantity is not None:
                    return quantity
    return None

def _position_source_key(position: TenderPosition) -> str:
    reference = position.sourceReference
    if reference is not None and reference.row is not None:
        table_scope = reference.sheet or reference.table
        if reference.fileName and table_scope:
            return ":".join(
                (
                    "source",
                    _word_table_key(reference.fileName),
                    _word_table_key(table_scope),
                    str(reference.row),
                )
            )
        if reference.fileName and reference.productColumn:
            return ":".join(
                (
                    "source",
                    _word_table_key(reference.fileName),
                    str(reference.row),
                    _word_table_key(reference.productColumn),
                )
            )
    if position.candidateId:
        return f"candidate:{position.candidateId}"
    return ""

def _position_unit_key(value: Any) -> str:
    unit = _word_table_key(value)
    if re.fullmatch(r"\u0448\u0442(?:\u0443\u043a\u0430|\u0443\u043a\u0438|\u0443\u043a)?", unit):
        return "\u0448\u0442"
    return unit

def _same_replicated_product(left: TenderPosition, right: TenderPosition) -> bool:
    left_key = _position_name_key(left)
    right_key = _position_name_key(right)
    return bool(left_key and right_key) and (
        left_key == right_key
        or left_key.startswith(f"{right_key} ")
        or right_key.startswith(f"{left_key} ")
    )

def _cross_table_product_identity(position: TenderPosition) -> str:
    """Normalize equivalent table labels without weakening same-table rows."""
    value = _clean(position.product).casefold().replace("ё", "е")
    while True:
        trimmed = _PRODUCT_COPY_TAIL_PATTERN.sub("", value)
        if trimmed == value:
            break
        value = trimmed
    value = re.sub(r"(\d)\s+([a-zа-я])", r"\1\2", value)
    value = re.sub(r"\s*([/-])\s*", r"\1", value)
    return re.sub(r"[^a-zа-я0-9,./-]+", " ", value).strip()

def _cross_document_model_identity(position: TenderPosition) -> str:
    """Return a stable article/model key despite Cyrillic/Latin look-alikes."""
    value = unicodedata.normalize("NFKC", _clean(position.product)).casefold()
    value = _MODEL_ROW_SUFFIX_PATTERN.sub(" ", value)
    for word in _MODEL_GENERIC_WORDS:
        value = value.replace(word, " ")
    value = value.translate(_MODEL_LOOKALIKE_TRANSLATION)
    identity = "".join(re.findall(r"[a-z0-9]+", value))
    # A plain generic label must never be used to collapse distinct products.
    return identity if len(identity) >= 8 and sum(char.isdigit() for char in identity) >= 2 else ""

def _merge_cross_document_model_copies(
    positions: list[TenderPosition],
    skipped_indexes: set[int],
) -> int:
    """Merge source copies that differ only in script/order around a model.

    This targets lists such as ``BA57`` in a technical PDF versus ``ВА57`` in
    the NMC workbook. Rows from one table remain separate, and explicitly
    different lots are never compared.
    """
    groups: dict[
        tuple[str, float | None, str, str],
        dict[tuple[str, str], list[tuple[int, TenderPosition]]],
    ] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        identity = _cross_document_model_identity(position)
        scope = _source_table_scope(position)
        if not identity or not scope[0]:
            continue
        lot = _word_table_key(position.lotNumber)
        groups.setdefault(
            (identity, position.quantity, _position_unit_key(position.unit), lot), {}
        ).setdefault(scope, []).append((index, position))

    merged_count = 0
    for scopes in groups.values():
        if len(scopes) < 2:
            continue
        ordered_scopes = sorted(
            scopes,
            key=lambda scope: (
                min(
                    source_role_priority(_position_source_role(position))
                    for _, position in scopes[scope]
                ),
                min(index for index, _ in scopes[scope]),
            ),
        )
        canonical_rows = list(scopes[ordered_scopes[0]])
        for duplicate_scope in ordered_scopes[1:]:
            for pair_index, ((canonical_index, canonical), (duplicate_index, duplicate)) in enumerate(
                zip(canonical_rows, scopes[duplicate_scope])
            ):
                if canonical_index in skipped_indexes or duplicate_index in skipped_indexes:
                    continue
                merged = _merge_replicated_position_details(canonical, duplicate)
                positions[canonical_index] = merged
                canonical_rows[pair_index] = (
                    canonical_index,
                    merged,
                )
                skipped_indexes.add(duplicate_index)
                merged_count += 1
    return merged_count

def _source_table_scope(position: TenderPosition) -> tuple[str, str]:
    reference = position.sourceReference
    if reference is None:
        return ("", "")
    return (
        _word_table_key(reference.fileName),
        _word_table_key(reference.sheet or reference.table) or "__document_text__",
    )

def _same_source_row_product_variant(left: TenderPosition, right: TenderPosition) -> bool:
    left_reference = left.sourceReference
    right_reference = right.sourceReference
    if (
        left_reference is None
        or right_reference is None
        or left_reference.row is None
        or right_reference.row is None
        or _word_table_key(left_reference.fileName) != _word_table_key(right_reference.fileName)
        or left_reference.row != right_reference.row
        or left.quantity != right.quantity
        or _position_unit_key(left.unit) != _position_unit_key(right.unit)
    ):
        return False
    if _same_replicated_product(left, right):
        return True
    return len(_product_name_stems(left) & _product_name_stems(right)) >= 2

def _is_generic_product_copy(position: TenderPosition) -> bool:
    name = _clean(position.product).casefold()
    stems = _product_name_stems(position)
    return "поставка" in name or len(stems) <= 1

def _generic_copy_matches_detail(generic: TenderPosition, detailed: TenderPosition) -> bool:
    if generic.quantity != detailed.quantity or _position_unit_key(generic.unit) != _position_unit_key(detailed.unit):
        return False
    generic_stems = _product_name_stems(generic)
    detailed_stems = _product_name_stems(detailed)
    if not generic_stems or not detailed_stems:
        return False
    shared = generic_stems & detailed_stems
    return len(shared) >= min(2, len(generic_stems))

def _power_transformer_identity(position: TenderPosition) -> tuple[str, str] | None:
    """Return the stable series/capacity pair used in replicated transformer tables."""
    text = " ".join(
        (
            position.product,
            position.productQuery or "",
            *(item.value for item in position.characteristics),
        )
    )
    series = _POWER_TRANSFORMER_SERIES_PATTERN.search(text)
    capacity = _POWER_TRANSFORMER_CAPACITY_PATTERN.search(text)
    if series is None or capacity is None:
        return None
    return series.group(0).casefold(), capacity.group(1)

def _cross_document_position_key(position: TenderPosition) -> tuple[str, str, float | None, str] | None:
    reference = position.sourceReference
    if reference is None or not _clean(reference.fileName):
        return None
    product = _position_name_key(position)
    if not product:
        return None
    strong_identity = (
        _word_table_key(position.article)
        or _word_table_key(position.model)
        or (
            f"{_word_table_key(position.lotNumber)}:{_word_table_key(position.positionNumber)}"
            if position.lotNumber and position.positionNumber
            else ""
        )
    )
    lot = _word_table_key(position.lotNumber)
    if lot:
        strong_identity = f"lot:{lot}:{strong_identity}"
    return (product, strong_identity, position.quantity, _position_unit_key(position.unit))

def _same_master_and_allocation_product(
    master: TenderPosition,
    allocation: TenderPosition,
) -> bool:
    """Confirm a safe identity match across a TЗ master and NMC allocation.

    Quantities intentionally do *not* participate: an NMC can split the total
    from the technical specification between branches or delivery addresses.
    Exact normalized title (or model/article) and compatible units are still
    required, so similarly named but different goods are not collapsed.
    """
    if _position_unit_key(master.unit) != _position_unit_key(allocation.unit):
        return False
    master_lot = _word_table_key(master.lotNumber)
    allocation_lot = _word_table_key(allocation.lotNumber)
    if master_lot and allocation_lot and master_lot != allocation_lot:
        return False

    master_article = _word_table_key(master.article)
    allocation_article = _word_table_key(allocation.article)
    if master_article and master_article == allocation_article:
        return True
    master_model = _word_table_key(master.model)
    allocation_model = _word_table_key(allocation.model)
    if master_model and master_model == allocation_model:
        return True

    master_identity = _cross_table_product_identity(master)
    allocation_identity = _cross_table_product_identity(allocation)
    # One-word generic labels (for example, just "кабель") are too weak for
    # an automatic merge unless model/article confirmed the match above.
    return bool(
        master_identity
        and master_identity == allocation_identity
        and len(master_identity.split()) >= 2
    )

def _merge_branch_allocations_into_technical_masters(
    positions: list[TenderPosition],
    skipped_indexes: set[int],
) -> int:
    """Collapse NMC branch/address allocations into their TЗ master row.

    A price justification often repeats one purchased product for each
    филиал/адрес. These rows are logistics allocations, not independent
    catalog items. The rule is intentionally narrow: there must be exactly
    one matching technical-specification position and at least two matching
    NMC rows. If the allocation sum differs from the TЗ total, the TЗ total
    remains authoritative and a reconciliation conflict is attached for the
    product auditor/debug output.
    """
    technical_indexes = [
        index
        for index, position in enumerate(positions)
        if index not in skipped_indexes
        and _position_source_role(position) == TECHNICAL_ROLE
        and position.quantity is not None
    ]
    allocations_by_master: dict[int, list[int]] = {}
    for allocation_index, allocation in enumerate(positions):
        if (
            allocation_index in skipped_indexes
            or _position_source_role(allocation) != PRICE_ROLE
            or allocation.quantity is None
        ):
            continue
        matches = [
            master_index
            for master_index in technical_indexes
            if _source_table_scope(positions[master_index])
            != _source_table_scope(allocation)
            and _same_master_and_allocation_product(
                positions[master_index], allocation
            )
        ]
        if len(matches) == 1:
            allocations_by_master.setdefault(matches[0], []).append(allocation_index)

    merged_count = 0
    for master_index, allocation_indexes in allocations_by_master.items():
        # One NMC row may merely be a document copy. Existing cross-document
        # logic handles that case. Several rows are required before treating
        # them as a филиал/адрес breakdown.
        if len(allocation_indexes) < 2:
            continue
        master = positions[master_index]
        allocation_rows = [positions[index] for index in allocation_indexes]
        allocation_total = sum(float(item.quantity or 0) for item in allocation_rows)
        for allocation_index in allocation_indexes:
            master = _merge_replicated_position_details(
                master,
                positions[allocation_index],
            )
            skipped_indexes.add(allocation_index)
            merged_count += 1

        reference = master.sourceReference
        if reference is not None:
            fact_type = (
                "allocation_breakdown_equals_total"
                if abs(float(master.quantity or 0) - allocation_total) < 1e-9
                else "allocation_breakdown_conflicts_with_total"
            )
            fact = ProductReconciliationFact(
                factId=(
                    "allocation-breakdown:"
                    f"{_source_table_scope(master)[0]}:"
                    f"{_source_table_scope(master)[1]}:"
                    f"{reference.row or master_index}"
                ),
                type=fact_type,
                fileName=reference.fileName,
                table=reference.sheet or reference.table,
                sectionRole=reference.sectionRole,
                product=master.product,
                unit=master.unit,
                totalQuantity=float(master.quantity or 0),
                detailQuantitySum=allocation_total,
                detailRows=[
                    {
                        "fileName": item.sourceReference.fileName
                        if item.sourceReference is not None
                        else "",
                        "table": (
                            item.sourceReference.sheet or item.sourceReference.table
                            if item.sourceReference is not None
                            else ""
                        ),
                        "row": item.sourceReference.row
                        if item.sourceReference is not None
                        else None,
                        "quantity": item.quantity,
                        "sourceCells": item.sourceCells,
                    }
                    for item in allocation_rows
                ],
            )
            facts = list(master.reconciliationFacts)
            if not any(existing.factId == fact.factId for existing in facts):
                facts.append(fact)
                master = master.model_copy(update={"reconciliationFacts": facts})
        positions[master_index] = master
    return merged_count

def _merge_technical_subset_into_price_position(
    price_position: TenderPosition,
    technical_position: TenderPosition,
) -> TenderPosition:
    """Attach only technical evidence while keeping the NMC row canonical.

    A technical-specification table can list just part of a larger NMC table.
    In that case the NMC row remains the purchase position (and retains its
    name, quantity, price, and primary sourceReference); the technical row
    contributes only product characteristics and its source reference.
    """
    characteristics = list(price_position.characteristics)
    seen_characteristics = {
        (_word_table_key(item.name), _word_table_key(item.value))
        for item in characteristics
    }
    for characteristic in technical_position.characteristics:
        key = (
            _word_table_key(characteristic.name),
            _word_table_key(characteristic.value),
        )
        if key not in seen_characteristics:
            characteristics.append(characteristic)
            seen_characteristics.add(key)

    source_references = (
        _all_source_references(price_position)
        + _all_source_references(technical_position)
    )
    deduplicated_references: list[ProductSourceReference] = []
    seen_references: set[tuple[str, str, int | None, str]] = set()
    for reference in source_references:
        key = _source_reference_key(reference)
        if key in seen_references:
            continue
        seen_references.add(key)
        deduplicated_references.append(reference)

    updates: dict[str, Any] = {}
    if characteristics != price_position.characteristics:
        updates["characteristics"] = characteristics
    if deduplicated_references != price_position.sourceReferences:
        updates["sourceReferences"] = deduplicated_references
    return price_position.model_copy(update=updates) if updates else price_position

def _technical_subset_pairs_in_price_table(
    positions: list[TenderPosition],
    skipped_indexes: set[int],
) -> list[tuple[int, int]]:
    """Return (NMC, TЗ) pairs when the whole TЗ table is a NMC subset.

    A match must preserve quantity/unit when both are present and have a
    strong product identity.  Requiring every TЗ row to match prevents a
    partial false-positive from silently deleting an independent position.
    """
    by_scope: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        role = _position_source_role(position)
        if role not in {PRICE_ROLE, TECHNICAL_ROLE}:
            continue
        scope = _source_table_scope(position)
        if scope[0]:
            by_scope.setdefault(scope, []).append((index, position))

    price_scopes = [
        scope for scope, rows in by_scope.items()
        if rows and _position_source_role(rows[0][1]) == PRICE_ROLE
    ]
    technical_scopes = [
        scope for scope, rows in by_scope.items()
        if rows and _position_source_role(rows[0][1]) == TECHNICAL_ROLE
    ]
    pairs: list[tuple[int, int]] = []
    consumed_scopes: set[tuple[str, str]] = set()

    for price_scope in price_scopes:
        price_rows = by_scope[price_scope]
        for technical_scope in technical_scopes:
            if technical_scope in consumed_scopes:
                continue
            technical_rows = by_scope[technical_scope]
            if not technical_rows or len(technical_rows) >= len(price_rows):
                continue

            available = list(price_rows)
            table_pairs: list[tuple[int, int]] = []
            for technical_index, technical in technical_rows:
                matches: list[tuple[int, TenderPosition]] = []
                for price_index, price in available:
                    if (
                        price.quantity is not None
                        and technical.quantity is not None
                        and price.quantity != technical.quantity
                    ):
                        continue
                    if (
                        _clean(price.unit)
                        and _clean(technical.unit)
                        and _position_unit_key(price.unit)
                        != _position_unit_key(technical.unit)
                    ):
                        continue
                    same_identity = (
                        _cross_table_product_identity(price)
                        == _cross_table_product_identity(technical)
                        or _same_replicated_product(price, technical)
                        or (
                            _power_transformer_identity(price) is not None
                            and _power_transformer_identity(price)
                            == _power_transformer_identity(technical)
                        )
                    )
                    if same_identity:
                        matches.append((price_index, price))
                if len(matches) != 1:
                    table_pairs = []
                    break
                price_match = matches[0]
                table_pairs.append((price_match[0], technical_index))
                available.remove(price_match)

            if len(table_pairs) == len(technical_rows):
                pairs.extend(table_pairs)
                consumed_scopes.add(technical_scope)
    return pairs

def _aligned_section_duplicate_pairs(
    positions: list[TenderPosition],
) -> list[tuple[int, int]]:
    by_scope: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        reference = position.sourceReference
        if reference is None:
            continue
        file_key = _word_table_key(reference.fileName)
        role = _position_source_role(position)
        if not file_key or role not in {"price_justification", "technical_specification"}:
            continue
        by_scope.setdefault((file_key, role), []).append((index, position))

    options: list[tuple[float, float, tuple[str, str], tuple[str, str]]] = []
    price_scopes = [scope for scope in by_scope if scope[1] == "price_justification"]
    technical_scopes = [
        scope for scope in by_scope if scope[1] == "technical_specification"
    ]
    for price_scope in price_scopes:
        price_rows = by_scope[price_scope]
        for technical_scope in technical_scopes:
            technical_rows = by_scope[technical_scope]
            if len(price_rows) != len(technical_rows) or len(price_rows) < 4:
                continue
            aligned_identity = 0
            aligned_grid = 0
            for (_, price), (_, technical) in zip(price_rows, technical_rows):
                same_quantity = price.quantity == technical.quantity
                same_unit = _position_unit_key(price.unit) == _position_unit_key(
                    technical.unit
                )
                if same_quantity and same_unit:
                    aligned_grid += 1
                    if _position_name_key(price) == _position_name_key(technical):
                        aligned_identity += 1
            identity_ratio = aligned_identity / len(price_rows)
            grid_ratio = aligned_grid / len(price_rows)
            if identity_ratio >= 0.75 and grid_ratio >= 0.90:
                options.append(
                    (identity_ratio, grid_ratio, price_scope, technical_scope)
                )

    pairs: list[tuple[int, int]] = []
    used_price_scopes: set[tuple[str, str]] = set()
    used_technical_scopes: set[tuple[str, str]] = set()
    for _, _, price_scope, technical_scope in sorted(options, reverse=True):
        if (
            price_scope in used_price_scopes
            or technical_scope in used_technical_scopes
        ):
            continue
        used_price_scopes.add(price_scope)
        used_technical_scopes.add(technical_scope)
        pairs.extend(
            (technical_entry[0], price_entry[0])
            for price_entry, technical_entry in zip(
                by_scope[price_scope],
                by_scope[technical_scope],
            )
        )
    return pairs

def _aligned_replicated_table_pairs(
    positions: list[TenderPosition],
    skipped_indexes: set[int],
) -> list[tuple[int, int]]:
    """Pair full table copies across documents without collapsing rows in one table."""
    by_scope: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes:
            continue
        reference = position.sourceReference
        if reference is None:
            continue
        file_name = _word_table_key(reference.fileName) or "__single_document__"
        table_scope = _word_table_key(reference.sheet or reference.table)
        if not table_scope:
            continue
        by_scope.setdefault((file_name, table_scope), []).append((index, position))

    scopes = list(by_scope)
    options: list[tuple[float, float, tuple[str, str], tuple[str, str]]] = []
    for left_offset, left_scope in enumerate(scopes):
        left_rows = by_scope[left_scope]
        is_identified_single_row = (
            len(left_rows) == 1
            and bool(re.search(r"\d", _position_name_key(left_rows[0][1])))
        )
        if len(left_rows) < 2 and not is_identified_single_row:
            continue
        for right_scope in scopes[left_offset + 1 :]:
            right_rows = by_scope[right_scope]
            if len(left_rows) != len(right_rows):
                continue
            aligned_grid = 0
            strong_identity = 0
            for (_, left), (_, right) in zip(left_rows, right_rows):
                if left.quantity != right.quantity or _position_unit_key(left.unit) != _position_unit_key(right.unit):
                    continue
                aligned_grid += 1
                left_identity = _power_transformer_identity(left)
                right_identity = _power_transformer_identity(right)
                left_stems = _product_name_stems(left)
                right_stems = _product_name_stems(right)
                exact_model = bool(
                    _clean(left.model or left.article)
                    and _word_table_key(left.model or left.article)
                    == _word_table_key(right.model or right.article)
                )
                if (
                    _same_replicated_product(left, right)
                    or exact_model
                    or (left_identity is not None and left_identity == right_identity)
                    or len(left_stems & right_stems) >= 2
                ):
                    strong_identity += 1
            grid_ratio = aligned_grid / len(left_rows)
            identity_ratio = strong_identity / len(left_rows)
            # A two-row table must match both rows. Longer tables allow one
            # noisy row, but never rely on quantity/unit alone.
            required_identity = 1.0 if len(left_rows) <= 3 else 0.80
            if grid_ratio >= 0.90 and identity_ratio >= required_identity:
                options.append((identity_ratio, grid_ratio, left_scope, right_scope))

    pairs: list[tuple[int, int]] = []
    used_scopes: set[tuple[str, str]] = set()
    for _, _, left_scope, right_scope in sorted(options, reverse=True):
        if left_scope in used_scopes or right_scope in used_scopes:
            continue
        left_rows = by_scope[left_scope]
        right_rows = by_scope[right_scope]
        left_priority = min(
            source_role_priority(_position_source_role(position))
            for _, position in left_rows
        )
        right_priority = min(
            source_role_priority(_position_source_role(position))
            for _, position in right_rows
        )
        left_first_index = left_rows[0][0]
        right_first_index = right_rows[0][0]
        if (left_priority, left_first_index) <= (right_priority, right_first_index):
            canonical_rows, duplicate_rows = left_rows, right_rows
        else:
            canonical_rows, duplicate_rows = right_rows, left_rows
        pairs.extend(
            (canonical[0], duplicate[0])
            for canonical, duplicate in zip(canonical_rows, duplicate_rows)
        )
        used_scopes.update((left_scope, right_scope))
    return pairs

def _merge_partial_cross_table_copies(
    positions: list[TenderPosition],
    skipped_indexes: set[int],
) -> int:
    """Merge exact product copies split across TЗ, NMC and contract tables.

    Full-table alignment is intentionally not required here: a contract can
    repeat only part of a price list. Rows from one source table are never
    merged with each other, preserving repeated purchase positions.
    """
    groups: dict[tuple[str, float | None, str], dict[tuple[str, str], list[tuple[int, TenderPosition]]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        identity = _cross_table_product_identity(position)
        scope = _source_table_scope(position)
        if not identity or not scope[0]:
            continue
        groups.setdefault(
            (identity, position.quantity, _position_unit_key(position.unit)), {}
        ).setdefault(scope, []).append((index, position))

    merged_count = 0
    for scopes in groups.values():
        if len(scopes) < 2:
            continue
        ordered_scopes = sorted(
            scopes,
            key=lambda scope: (
                min(source_role_priority(_position_source_role(position)) for _, position in scopes[scope]),
                min(index for index, _ in scopes[scope]),
            ),
        )
        canonical_entries = list(scopes[ordered_scopes[0]])
        for duplicate_scope in ordered_scopes[1:]:
            for offset, (duplicate_index, duplicate) in enumerate(scopes[duplicate_scope]):
                if offset >= len(canonical_entries):
                    # Keep an unmatched occurrence: it may be a real repeated
                    # row in the other table.
                    continue
                canonical_index, canonical = canonical_entries[offset]
                canonical = _merge_replicated_position_details(canonical, duplicate)
                positions[canonical_index] = canonical
                canonical_entries[offset] = (canonical_index, canonical)
                skipped_indexes.add(duplicate_index)
                merged_count += 1
    return merged_count

def _deduplicate_cross_document_positions(
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Keep every same-file row, but retain one document's copy of replicated rows."""
    skipped_indexes: set[int] = set()
    warnings: list[str] = []
    aligned_pairs = _aligned_section_duplicate_pairs(positions)
    for canonical_index, duplicate_index in aligned_pairs:
        positions[canonical_index] = _merge_replicated_position_details(
            positions[canonical_index],
            positions[duplicate_index],
        )
        skipped_indexes.add(duplicate_index)
    if aligned_pairs:
        warnings.append(
            "Paired replicated price-justification and technical-specification "
            "rows by aligned position order; retained the technical row and "
            "merged NMC price data."
        )

    technical_subset_pairs = _technical_subset_pairs_in_price_table(
        positions, skipped_indexes
    )
    for price_index, technical_index in technical_subset_pairs:
        positions[price_index] = _merge_technical_subset_into_price_position(
            positions[price_index], positions[technical_index]
        )
        skipped_indexes.add(technical_index)
    if technical_subset_pairs:
        warnings.append(
            "Recognized the technical-specification table as a subset of the "
            "NMC table; retained all NMC rows and attached only technical "
            "characteristics and source references to matching rows."
        )

    table_pairs = _aligned_replicated_table_pairs(positions, skipped_indexes)
    for canonical_index, duplicate_index in table_pairs:
        positions[canonical_index] = _merge_replicated_position_details(
            positions[canonical_index],
            positions[duplicate_index],
        )
        skipped_indexes.add(duplicate_index)
    if table_pairs:
        warnings.append(
            "Merged replicated table copies after aligned row order, quantity, "
            "unit, and stable product identities were confirmed."
        )

    branch_allocation_count = _merge_branch_allocations_into_technical_masters(
        positions,
        skipped_indexes,
    )
    if branch_allocation_count:
        warnings.append(
            "Collapsed NMC branch/address allocation rows into their matching "
            "technical-specification product totals."
        )

    partial_copy_count = _merge_partial_cross_table_copies(positions, skipped_indexes)
    if partial_copy_count:
        warnings.append(
            "Merged partial copies across technical specification, price justification, "
            "and contract tables by normalized product identity, quantity, and unit."
        )

    model_copy_count = _merge_cross_document_model_copies(positions, skipped_indexes)
    if model_copy_count:
        warnings.append(
            "Merged replicated product rows across documents by model/article, "
            "quantity, and unit despite Cyrillic/Latin label variants."
        )

    # Some tenders repeat only two or three technical positions. The generic
    # aligned-table safeguard deliberately leaves such short lists alone.
    # For power transformers, however, series plus nominal capacity is a
    # stable identity: it lets us merge copies from price, technical, and
    # contract tables without collapsing different transformer executions.
    transformer_groups: dict[tuple[str, str, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        identity = _power_transformer_identity(position)
        reference = position.sourceReference
        scope = _word_table_key(reference.sheet or reference.table)
        if identity is None or not scope:
            continue
        transformer_groups.setdefault(
            (*identity, position.quantity, _position_unit_key(position.unit)), []
        ).append((index, position))

    transformer_duplicates = 0
    for group in transformer_groups.values():
        scopes = {
            (_word_table_key(position.sourceReference.fileName), _word_table_key(position.sourceReference.sheet or position.sourceReference.table))
            for _, position in group
            if position.sourceReference is not None
        }
        # A repeated item within one source table remains a separate tender row.
        if len(group) < 2 or len(scopes) < 2 or len(scopes) != len(group):
            continue
        canonical_index, canonical = min(
            group,
            key=lambda entry: (
                source_role_priority(_position_source_role(entry[1])),
                entry[0],
            ),
        )
        for duplicate_index, duplicate in group:
            if duplicate_index == canonical_index:
                continue
            canonical = _merge_replicated_position_details(canonical, duplicate)
            positions[canonical_index] = canonical
            skipped_indexes.add(duplicate_index)
            transformer_duplicates += 1
    if transformer_duplicates:
        warnings.append(
            "Merged replicated power-transformer rows by stable series and nominal capacity."
        )

    same_row_groups: dict[tuple[str, int, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        reference = position.sourceReference
        if reference.row is None:
            continue
        same_row_groups.setdefault(
            (
                _word_table_key(reference.fileName),
                reference.row,
                position.quantity,
                _position_unit_key(position.unit),
            ),
            [],
        ).append((index, position))

    same_row_duplicates = 0
    for group in same_row_groups.values():
        if len(group) < 2:
            continue
        canonical_index, canonical = max(
            group,
            key=lambda entry: (
                len(_clean(entry[1].product)),
                len(entry[1].characteristics),
                -entry[0],
            ),
        )
        for duplicate_index, duplicate in group:
            if duplicate_index == canonical_index or not _same_source_row_product_variant(canonical, duplicate):
                continue
            canonical = _merge_replicated_position_details(canonical, duplicate)
            positions[canonical_index] = canonical
            skipped_indexes.add(duplicate_index)
            same_row_duplicates += 1
    if same_row_duplicates:
        warnings.append(
            "Merged repeated product variants extracted from the same source row."
        )

    source_groups: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        reference = position.sourceReference
        scope = _word_table_key(reference.sheet or reference.table) or "__document_text__"
        source_groups.setdefault((_word_table_key(reference.fileName), scope), []).append(
            (index, position)
        )

    generic_table_pairs: list[tuple[int, int]] = []
    scopes = list(source_groups)
    consumed_generic_scopes: set[tuple[str, str]] = set()
    for generic_scope in scopes:
        generic_rows = source_groups[generic_scope]
        if generic_scope in consumed_generic_scopes or not generic_rows:
            continue
        if not all(_is_generic_product_copy(position) for _, position in generic_rows):
            continue
        matches: list[tuple[int, tuple[str, str], list[tuple[int, TenderPosition]]]] = []
        for detailed_scope in scopes:
            if detailed_scope == generic_scope:
                continue
            detailed_rows = source_groups[detailed_scope]
            if len(detailed_rows) != len(generic_rows) or not detailed_rows:
                continue
            if all(
                _generic_copy_matches_detail(generic, detailed)
                for (_, generic), (_, detailed) in zip(generic_rows, detailed_rows)
            ):
                specificity = sum(
                    len(_clean(position.product)) + len(position.characteristics) * 80
                    for _, position in detailed_rows
                )
                matches.append((specificity, detailed_scope, detailed_rows))
        if not matches:
            continue
        _, _, detailed_rows = max(matches, key=lambda item: item[0])
        generic_table_pairs.extend(
            (detailed_index, generic_index)
            for (generic_index, _), (detailed_index, _) in zip(generic_rows, detailed_rows)
        )
        consumed_generic_scopes.add(generic_scope)

    for canonical_index, duplicate_index in generic_table_pairs:
        positions[canonical_index] = _merge_replicated_position_details(
            positions[canonical_index], positions[duplicate_index]
        )
        skipped_indexes.add(duplicate_index)
    if generic_table_pairs:
        warnings.append(
            "Merged a generic product-table copy into the matching detailed table."
        )

    grouped: dict[tuple[str, str, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes:
            continue
        key = _cross_document_position_key(position)
        if key is not None:
            grouped.setdefault(key, []).append((index, position))

    for group in grouped.values():
        by_file: dict[str, list[tuple[int, TenderPosition]]] = {}
        for index, position in group:
            file_name = _clean(position.sourceReference.fileName if position.sourceReference else "")
            source_scope = f"{file_name}::{_position_source_role(position)}"
            by_file.setdefault(source_scope, []).append((index, position))
        if len(by_file) < 2:
            continue
        canonical_file = min(
            by_file,
            key=lambda name: (
                min(
                    source_role_priority(_position_source_role(position))
                    for _, position in by_file[name]
                ),
                min(index for index, _ in by_file[name]),
            ),
        )
        canonical_copies = by_file[canonical_file]
        for file_name, copies in by_file.items():
            if file_name == canonical_file:
                continue
            for copy_offset, (_, duplicate) in enumerate(copies):
                if not canonical_copies:
                    break
                canonical_index, canonical = canonical_copies[
                    min(copy_offset, len(canonical_copies) - 1)
                ]
                updated = _merge_replicated_position_details(canonical, duplicate)
                if updated != canonical:
                    group_entry = (canonical_index, updated)
                    canonical_copies[
                        min(copy_offset, len(canonical_copies) - 1)
                    ] = group_entry
                    positions[canonical_index] = updated
            skipped_indexes.update(index for index, _ in copies)
            warnings.append(
                "Skipped replicated product rows from document "
                f"{file_name}; retained {canonical_file}."
            )
    return (
        [position for index, position in enumerate(positions) if index not in skipped_indexes],
        list(dict.fromkeys(warnings)),
    )

def _is_tender_subject_or_survey_title(position: TenderPosition) -> bool:
    """Reject tender-level labels and LLM guesses from an OL document title."""
    product = _clean(position.product)
    if _TENDER_SUBJECT_PRODUCT_PATTERN.fullmatch(product):
        return True

    reference = position.sourceReference
    return bool(
        position.source == "llm"
        and reference is not None
        and _SURVEY_SHEET_FILE_PATTERN.search(_clean(reference.fileName))
        and not _clean(reference.productColumn)
        and not _clean(position.model)
        and not _clean(position.article)
        and not position.sourceCells
    )

def _is_blank_okpd_template_position(position: TenderPosition) -> bool:
    """Recognize an LLM label inferred from a blank offer-form product cell."""
    reference = position.sourceReference
    return bool(
        position.source == "llm"
        and reference is not None
        and reference.row is not None
        and _position_source_role(position) in {OTHER_ROLE, COMPOSITE_ROLE}
        and _OKPD_PLACEHOLDER_PRODUCT_PATTERN.fullmatch(_clean(position.product))
        and not _clean(position.model)
        and not _clean(position.article)
        and not position.characteristics
        and not _clean(position.requirements)
        and not position.sourceCells
    )

def _contiguous_template_groups(
    positions: list[TenderPosition],
) -> list[list[tuple[int, TenderPosition]]]:
    grouped: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if not _is_blank_okpd_template_position(position):
            continue
        reference = position.sourceReference
        if reference is None:
            continue
        key = (
            _word_table_key(reference.fileName),
            _word_table_key(reference.table) or "__document__",
        )
        grouped.setdefault(key, []).append((index, position))

    result: list[list[tuple[int, TenderPosition]]] = []
    for entries in grouped.values():
        current: list[tuple[int, TenderPosition]] = []
        previous_row: int | None = None
        for entry in entries:
            row = entry[1].sourceReference.row if entry[1].sourceReference else None
            if current and (row is None or previous_row is None or row != previous_row + 1):
                result.append(current)
                current = []
            current.append(entry)
            previous_row = row
        if current:
            result.append(current)
    return result

def _technical_position_groups(
    positions: list[TenderPosition],
) -> list[list[TenderPosition]]:
    grouped: dict[tuple[str, str], list[TenderPosition]] = {}
    for position in positions:
        reference = position.sourceReference
        if (
            reference is None
            or reference.row is None
            or _position_source_role(position) != TECHNICAL_ROLE
            or _is_blank_okpd_template_position(position)
        ):
            continue
        key = (
            _word_table_key(reference.fileName),
            _word_table_key(reference.table) or "__document__",
        )
        grouped.setdefault(key, []).append(position)
    return [
        sorted(group, key=lambda position: position.sourceReference.row or 0)
        for group in grouped.values()
    ]

def _drop_blank_okpd_template_positions(
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Drop an aligned blank offer form when a detailed technical table exists."""
    skipped_indexes: set[int] = set()
    technical_groups = _technical_position_groups(positions)
    for template_group in _contiguous_template_groups(positions):
        if len(template_group) < 4:
            continue
        template_grid = [
            (position.quantity, _position_unit_key(position.unit))
            for _, position in template_group
        ]
        matching_group = next(
            (
                technical_group
                for technical_group in technical_groups
                if len(technical_group) == len(template_group)
                and [
                    (position.quantity, _position_unit_key(position.unit))
                    for position in technical_group
                ]
                == template_grid
            ),
            None,
        )
        if matching_group is not None:
            skipped_indexes.update(index for index, _ in template_group)

    if not skipped_indexes:
        return positions, []
    return (
        [position for index, position in enumerate(positions) if index not in skipped_indexes],
        [
            "Skipped a blank offer-form table inferred only from OKPD codes after "
            "an aligned technical specification confirmed the detailed product rows."
        ],
    )

def _apparel_family_key(position: TenderPosition) -> str:
    """Recognize a clothing family without treating its size grid as new goods."""
    value = _word_table_key(position.productQuery or position.product)
    if "\u043a\u043e\u0441\u0442\u044e\u043c" in value:
        product_type = "suit"
    elif "\u043a\u0443\u0440\u0442\u043a" in value:
        product_type = "jacket"
    else:
        return ""
    if "\u043c\u0443\u0436" in value:
        gender = "male"
    elif "\u0436\u0435\u043d" in value:
        gender = "female"
    else:
        return ""
    return f"{product_type}:{gender}:winter" if "\u0437\u0438\u043c" in value else ""

def _drop_unconfirmed_llm_quantity_breakdowns(
    llm_products: list[TenderPosition],
    source_backed_positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Discard an LLM-only clothing size breakdown when a table already has its total."""
    backed_by_family: dict[str, list[TenderPosition]] = {}
    for position in source_backed_positions:
        if not _position_source_key(position) or position.quantity is None:
            continue
        family = _apparel_family_key(position)
        if family:
            backed_by_family.setdefault(family, []).append(position)

    unconfirmed_by_family: dict[str, list[TenderPosition]] = {}
    for position in llm_products:
        if _position_source_key(position) or position.quantity is None:
            continue
        family = _apparel_family_key(position)
        if family:
            unconfirmed_by_family.setdefault(family, []).append(position)

    removed_ids: set[int] = set()
    warnings: list[str] = []
    for family, candidates in unconfirmed_by_family.items():
        total = sum(float(position.quantity or 0) for position in candidates)
        parent = next(
            (
                position
                for position in backed_by_family.get(family, [])
                if abs(float(position.quantity or 0) - total) < 1e-9
            ),
            None,
        )
        if parent is None or len(candidates) < 2:
            continue
        removed_ids.update(id(position) for position in candidates)
        warnings.append(
            "Skipped unreferenced LLM size breakdown of a source-backed position: "
            f"{parent.product[:160]} ({parent.quantity:g} = {total:g})."
        )
    return (
        [position for position in llm_products if id(position) not in removed_ids],
        warnings,
    )

def _drop_source_size_breakdowns_for_llm_totals(
    deterministic: list[TenderPosition],
    llm_products: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Keep an LLM total and attach proof for a same-table size breakdown.

    Some clothing tables contain a separate row for every size and a repeated
    ``total quantity`` value for the product family.  The LLM can correctly
    recover that total, while deterministic extraction sees only the size
    rows.  Without this reconciliation both representations reach
    autopodbor as independent products.

    This is intentionally narrow: rows must come from one table, explicitly
    contain a size/height characteristic, have the exact same normalized name
    and unit, and their quantities must add up to one LLM position. Repeated
    goods in different tables or lots are therefore left untouched.

    The source rows are collapsed deterministically, but their coordinates,
    sizes and arithmetic are attached to the surviving total as a
    ``reconciliationFact``. The subsequent product audit can therefore
    confirm the action or request manual review without rereading the tender.
    """
    grouped: dict[
        tuple[str, str, str, str], list[tuple[int, TenderPosition]]
    ] = {}
    for index, position in enumerate(deterministic):
        reference = position.sourceReference
        if (
            reference is None
            or reference.row is None
            or position.quantity is None
        ):
            continue
        scope = _source_table_scope(position)
        name = _position_name_key(position)
        unit = _position_unit_key(position.unit)
        if not scope[0] or not name:
            continue
        grouped.setdefault(
            (scope[0], scope[1], name, _word_table_key(position.lotNumber)), []
        ).append((index, position))

    def size_dimension(position: TenderPosition) -> tuple[str, str]:
        for characteristic in position.characteristics:
            header = _normalize_header(characteristic.name)
            if re.search(r"(?:^| )(?:размер|рост|размерный|типоразмер)(?: |$)", header):
                value = _clean(characteristic.value)
                if value:
                    return _clean(characteristic.name), value
        return "", ""

    skipped_indexes: set[int] = set()
    warnings: list[str] = []
    for (_, _, name, lot), entries in grouped.items():
        if len(entries) < 2:
            continue
        units = {_position_unit_key(position.unit) for _, position in entries}
        if len(units) != 1:
            continue
        unit = next(iter(units))
        total = sum(float(position.quantity or 0) for _, position in entries)
        size_rows = [size_dimension(position) for _, position in entries]
        if not all(value for _, value in size_rows):
            continue
        parent_index, parent = next(
            (
                (index, position)
                for index, position in enumerate(llm_products)
                if position.source == "llm"
                and position.quantity is not None
                and _position_name_key(position) == name
                and _position_unit_key(position.unit) in {"", unit}
                and _word_table_key(position.lotNumber) == lot
                and abs(float(position.quantity) - total) < 1e-9
            ),
            (None, None),
        )
        if parent is None:
            continue
        first_reference = entries[0][1].sourceReference
        if first_reference is None:
            continue
        size_column = next((header for header, _ in size_rows if header), "Размер")
        fact = ProductReconciliationFact(
            factId=(
                "size-breakdown:"
                f"{_source_table_scope(entries[0][1])[0]}:"
                f"{_source_table_scope(entries[0][1])[1]}:"
                f"{lot}:{first_reference.row}:{parent_index}"
            ),
            type="size_breakdown_equals_total",
            fileName=first_reference.fileName,
            table=first_reference.sheet or first_reference.table,
            sectionRole=first_reference.sectionRole,
            product=parent.product,
            unit=parent.unit or entries[0][1].unit,
            sizeColumn=size_column,
            totalQuantity=float(parent.quantity),
            detailQuantitySum=total,
            detailRows=[
                {
                    "candidateId": position.candidateId,
                    "row": position.sourceReference.row if position.sourceReference else None,
                    "size": size_value,
                    "quantity": position.quantity,
                    "sourceCells": position.sourceCells,
                }
                for (_, position), (_, size_value) in zip(entries, size_rows)
            ],
        )
        parent_facts = list(parent.reconciliationFacts)
        parent_facts.append(fact)
        llm_products[parent_index] = parent.model_copy(
            update={"reconciliationFacts": parent_facts}
        )
        skipped_indexes.update(index for index, _ in entries)
        warnings.append(
            "Collapsed source size breakdown into an LLM total and attached a "
            f"reconciliation fact: {parent.product[:160]} ({parent.quantity:g} = {total:g})."
        )
    return (
        [position for index, position in enumerate(deterministic) if index not in skipped_indexes],
        warnings,
    )

def _position_table_identity(position: TenderPosition) -> tuple[str, float | None, str]:
    return (
        _position_name_key(position),
        position.quantity,
        _position_unit_key(position.unit),
    )

def _source_backed_table_groups(
    positions: list[TenderPosition],
) -> list[list[tuple[int, TenderPosition]]]:
    grouped: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        reference = position.sourceReference
        if (
            reference is None
            or reference.row is None
            or not _clean(reference.fileName)
            or not (reference.sheet or reference.table)
        ):
            continue
        key = (
            _word_table_key(reference.fileName),
            _word_table_key(reference.sheet or reference.table),
        )
        grouped.setdefault(key, []).append((index, position))
    return [
        sorted(group, key=lambda entry: entry[1].sourceReference.row or 0)
        for group in grouped.values()
    ]

def _unreferenced_llm_runs(
    positions: list[TenderPosition],
) -> list[list[tuple[int, TenderPosition]]]:
    runs: list[list[tuple[int, TenderPosition]]] = []
    current: list[tuple[int, TenderPosition]] = []
    for index, position in enumerate(positions):
        if _position_source_key(position):
            if current:
                runs.append(current)
                current = []
            continue
        current.append((index, position))
    if current:
        runs.append(current)
    return runs

def _drop_unreferenced_llm_table_copies(
    deterministic: list[TenderPosition],
    llm_products: list[TenderPosition],
) -> tuple[list[TenderPosition], list[TenderPosition], list[str]]:
    """Remove a complete, source-less LLM copy of a structured table.

    The LLM can emit a valid copy of a table after unrelated document-level
    candidates. Whole-list alignment then cannot recognize it. Requiring a
    complete table, source-row ordering, and a four-row minimum avoids merging
    genuine repeated purchase positions inside the table.
    """
    removed_indexes: set[int] = set()
    warnings: list[str] = []
    for source_group in _source_backed_table_groups(deterministic):
        if len(source_group) < 4:
            continue
        source_identities = [
            _position_table_identity(position) for _, position in source_group
        ]
        if not all(identity[0] for identity in source_identities):
            continue
        for llm_run in _unreferenced_llm_runs(llm_products):
            if len(llm_run) < len(source_group):
                continue
            llm_identities = [
                _position_table_identity(position) for _, position in llm_run
            ]
            for start in range(len(llm_run) - len(source_group) + 1):
                end = start + len(source_group)
                if llm_identities[start:end] != source_identities:
                    continue
                for (source_index, source_position), (_, llm_position) in zip(
                    source_group, llm_run[start:end]
                ):
                    deterministic[source_index] = _merge_replicated_position_details(
                        source_position,
                        llm_position,
                    )
                removed_indexes.update(index for index, _ in llm_run[start:end])
                reference = source_group[0][1].sourceReference
                warnings.append(
                    "Skipped an unreferenced LLM copy of structured table rows; "
                    f"retained {reference.fileName if reference else 'source-backed'} rows."
                )
                break

    if not removed_indexes:
        return deterministic, llm_products, []
    return (
        deterministic,
        [
            position
            for index, position in enumerate(llm_products)
            if index not in removed_indexes
        ],
        list(dict.fromkeys(warnings)),
    )

def _merge_aligned_llm_table_copy(
    deterministic: list[TenderPosition],
    llm_products: list[TenderPosition],
) -> tuple[list[TenderPosition], list[TenderPosition], list[str]]:
    if len(deterministic) != len(llm_products) or len(deterministic) < 4:
        return deterministic, llm_products, []

    aligned_identity = 0
    aligned_grid = 0
    for source_position, llm_position in zip(deterministic, llm_products):
        same_quantity = source_position.quantity == llm_position.quantity
        same_unit = _word_table_key(source_position.unit) == _word_table_key(
            llm_position.unit
        )
        if same_quantity and same_unit:
            aligned_grid += 1
            if _position_name_key(source_position) == _position_name_key(llm_position):
                aligned_identity += 1
    identity_ratio = aligned_identity / len(deterministic)
    grid_ratio = aligned_grid / len(deterministic)
    if identity_ratio < 0.75 or grid_ratio < 0.90:
        return deterministic, llm_products, []

    merged = [
        _merge_replicated_position_details(source_position, llm_position)
        for source_position, llm_position in zip(deterministic, llm_products)
    ]
    return (
        merged,
        [],
        [
            "Skipped an aligned LLM copy of deterministic table rows; "
            "retained deterministic product identities and merged details."
        ],
    )

def merge_positions(
    deterministic: list[TenderPosition],
    llm_response: TenderPositionsResponse | None,
    seldon: list[TenderPosition] | None = None,
    max_positions: int = 5_000,
) -> tuple[list[TenderPosition], list[str]]:
    seldon = list(seldon or [])
    llm_products = list(llm_response.products if llm_response else [])
    deterministic, source_breakdown_warnings = _drop_source_size_breakdowns_for_llm_totals(
        deterministic,
        llm_products,
    )
    llm_products, breakdown_warnings = _drop_unconfirmed_llm_quantity_breakdowns(
        llm_products,
        deterministic + seldon + llm_products,
    )
    deterministic, llm_products, unreferenced_copy_warnings = (
        _drop_unreferenced_llm_table_copies(deterministic, llm_products)
    )
    deterministic, llm_products, aligned_copy_warnings = (
        _merge_aligned_llm_table_copy(deterministic, llm_products)
    )
    llm_product_ids = {id(position) for position in llm_products}
    combined = llm_products + seldon + deterministic
    warnings = (
        list(llm_response.warnings if llm_response else [])
        + source_breakdown_warnings
        + breakdown_warnings
        + unreferenced_copy_warnings
        + aligned_copy_warnings
    )
    seldon_by_source = {
        key: position
        for position in seldon
        if (key := _position_source_key(position))
    }
    excel_by_source = {
        key: position
        for position in deterministic
        if (key := _position_source_key(position))
    }
    if seldon and not any(position.quantity is not None for position in seldon):
        warnings.append(
            "Товарные позиции найдены в структурированных данных Seldon, но количество в них отсутствует."
        )
    result: list[TenderPosition] = []
    seen: dict[str, int] = {}
    for raw_position in combined:
        position = _normalize_product_description(_clear_tender_level_price(raw_position))
        if _is_noise_position(position):
            product_text = _clean(position.product)
            if _ADDRESS_OR_RECIPIENT_PATTERN.search(product_text):
                warnings.append(
                    "Пропущен адрес/получатель, ошибочно извлечённый как товар: "
                    f"{product_text[:200]}"
                )
            else:
                warnings.append(
                    "Пропущена служебная строка, ошибочно извлечённая как товар: "
                    f"{product_text[:200]}"
                )
            continue
        structured_quantity = _quantity_from_structured_evidence(position)
        if structured_quantity is not None and position.quantity != structured_quantity:
            warnings.append(
                "Количество позиции исправлено по соседним ячейкам Excel: "
                f"{_clean(position.product)[:200]} — {structured_quantity:g}."
            )
            position = position.model_copy(update={"quantity": structured_quantity})
        query = _clean(position.productQuery or position.product)
        source_key = _position_source_key(position)
        seldon_match = seldon_by_source.get(source_key)
        excel_match = excel_by_source.get(source_key)
        if (
            id(raw_position) in llm_product_ids
            and position.quantity is None
            and (deterministic or seldon)
            and seldon_match is None
            and excel_match is None
        ):
            warnings.append(
                "Skipped LLM product without quantity that was not confirmed by structured product rows: "
                f"{_clean(position.product)[:200]}"
            )
            continue
        if (
            id(raw_position) in llm_product_ids
            and (deterministic or seldon)
            and not source_key
            and not _clean(position.unit)
            and len(_position_name_key(position).split()) <= 2
            and not _clean(position.model)
            and not _clean(position.article)
            and not position.characteristics
        ):
            warnings.append(
                "Skipped unreferenced LLM category without a unit when structured "
                f"product rows are available: {_clean(position.product)[:200]}"
            )
            continue
        quantity = (
            seldon_match.quantity
            if seldon_match is not None and seldon_match.quantity is not None
            else excel_match.quantity
            if excel_match is not None and excel_match.quantity is not None
            else position.quantity
        )
        unit = (
            seldon_match.unit
            if seldon_match is not None and seldon_match.unit
            else excel_match.unit
            if excel_match is not None and excel_match.unit
            else position.unit
        )
        product = _clean(position.product)
        if not product:
            continue
        # Equal labels remain separate purchase positions unless they point to
        # the same source row. This preserves the coverage denominator.
        resolved_key = source_key or f"occurrence:{id(raw_position)}"
        existing_index = seen.get(resolved_key)
        if existing_index is not None:
            existing = result[existing_index]
            deterministic_replaces_llm = (
                position.source == "excel_table_deterministic"
                and existing.source != "excel_table_deterministic"
            )
            merged_existing = _merge_replicated_position_details(
                position if deterministic_replaces_llm else existing,
                existing if deterministic_replaces_llm else position,
            )
            if merged_existing != existing:
                result[existing_index] = merged_existing
                existing = merged_existing
            updates: dict[str, Any] = {}
            if existing.quantity is None and quantity is not None:
                updates["quantity"] = quantity
            if not existing.unit and unit:
                updates["unit"] = unit
            for field in (
                "requirements",
                "characteristics",
                "positionKey",
                "lotNumber",
                "positionNumber",
                "model",
                "documentUnitPriceRub",
                "documentLineTotalRub",
                "documentCurrency",
                "documentPriceEvidence",
                "documentPriceSource",
                "sourceReference",
                "sourceCells",
            ):
                existing_value = getattr(existing, field)
                candidate_value = getattr(position, field)
                if _missing(existing_value) and not _missing(candidate_value):
                    updates[field] = candidate_value
            candidate_evidence = _clean(position.evidence)
            existing_evidence = _clean(existing.evidence)
            if candidate_evidence and candidate_evidence not in existing_evidence:
                updates["evidence"] = _clean(
                    " | ".join(filter(None, (existing_evidence, candidate_evidence)))
                )[:1200]
            if updates:
                result[existing_index] = existing.model_copy(update=updates)
            seen[resolved_key] = existing_index
            if candidate_evidence or existing_evidence:
                warnings.append(
                    "Объединена повторно извлечённая товарная позиция из нескольких документов: "
                    f"{product[:200]}"
                )
            continue
        seen[resolved_key] = len(result)
        update = {"productQuery": query or product, "quantity": quantity, "unit": unit}
        if excel_match:
            for field in (
                "documentUnitPriceRub",
                "documentLineTotalRub",
                "documentCurrency",
                "documentPriceEvidence",
                "documentPriceSource",
            ):
                excel_value = getattr(excel_match, field)
                if not _missing(excel_value):
                    update[field] = excel_value
        result.append(position.model_copy(update=update))
        if len(result) >= max_positions:
            break
    result, blank_template_warnings = _drop_blank_okpd_template_positions(result)
    result, cross_document_warnings = _deduplicate_cross_document_positions(result)
    return result, warnings + blank_template_warnings + cross_document_warnings
