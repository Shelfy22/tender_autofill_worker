from app.config import Settings
from app.models import ParsedDocument, ProductSourceReference, TenderPosition
from app.services.document_analysis import build_document_analysis_units


def _settings(**overrides: object) -> Settings:
    return Settings(postgres_dsn="postgresql://user:pass@localhost/db", **overrides)


def test_document_analysis_units_are_source_oriented_not_recursive_chunks() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName="technical.pdf",
        documentKind="technical",
        text="A" * 12_000 + "\n\n" + "B" * 12_000,
        textQualityOk=True,
    )

    units, warnings = build_document_analysis_units(
        "Seldon page text",
        [document],
        [],
        _settings(document_analysis_unit_max_chars=10_000, document_analysis_max_units=10),
    )

    assert not warnings
    assert units[0].sourceType == "seldon_page"
    document_units = [unit for unit in units if unit.sourceType == "document"]
    assert [unit.partIndex for unit in document_units] == [1, 2, 3, 4]
    assert all("chunk" not in unit.unitId for unit in units)
    assert all(unit.inputSha256 for unit in units)


def test_spreadsheet_document_analysis_units_are_row_aware() -> None:
    document = ParsedDocument(
        documentIndex=2,
        fileName="spec.xlsx",
        documentKind="specification",
        text="spreadsheet text should not be copied into every unit",
        textQualityOk=True,
        spreadsheetTables=[{"sheet": "Лист1", "rows": []}],
    )
    positions = [
        TenderPosition(
            candidateId=f"xlsx:spec.xlsx:Лист1:{row}",
            product=f"Товар {row}",
            quantity=1,
            unit="шт",
            source="excel_table_deterministic",
            sourceReference=ProductSourceReference(
                fileName="spec.xlsx",
                sheet="Лист1",
                row=row,
                productColumn="B",
                quantityColumn="D",
                unitColumn="C",
                extractionMethod="excel_deterministic",
            ),
            sourceCells={"B": f"Товар {row}", "C": "шт", "D": "1"},
        )
        for row in range(2, 14)
    ]

    units, warnings = build_document_analysis_units(
        "",
        [document],
        positions,
        _settings(spreadsheet_candidate_review_max_rows=5),
    )

    assert not warnings
    assert [len(unit.spreadsheetCandidates) for unit in units] == [5, 5, 2]
    assert all(unit.sourceType == "spreadsheet" for unit in units)
    assert units[0].spreadsheetCandidates[0]["candidateId"] == "xlsx:spec.xlsx:Лист1:2"


def test_document_analysis_units_are_capped_with_warning() -> None:
    documents = [
        ParsedDocument(
            documentIndex=index,
            fileName=f"doc-{index}.txt",
            text="text",
            textQualityOk=True,
        )
        for index in range(1, 5)
    ]

    units, warnings = build_document_analysis_units(
        "page",
        documents,
        [],
        _settings(document_analysis_max_units=2),
    )

    assert len(units) == 2
    assert any("Document Analysis units limited" in warning for warning in warnings)


def test_document_analysis_default_keeps_large_file_as_single_unit() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName="large-spec.pdf",
        documentKind="technical",
        text="A" * 900_000,
        textQualityOk=True,
    )

    units, warnings = build_document_analysis_units(
        "",
        [document],
        [],
        _settings(),
    )

    assert not warnings
    assert len(units) == 1
    assert units[0].sourceType == "document"
    assert units[0].partIndex == 1
    assert units[0].partTotal == 1
    assert len(units[0].text) == 900_000


def test_document_analysis_default_splits_only_above_context_limit() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName="huge-spec.pdf",
        documentKind="technical",
        text="A" * 1_000_001,
        textQualityOk=True,
    )

    units, warnings = build_document_analysis_units(
        "",
        [document],
        [],
        _settings(),
    )

    assert not warnings
    assert [unit.partIndex for unit in units] == [1, 2]
    assert [unit.partTotal for unit in units] == [2, 2]
    assert len(units[0].text) == 1_000_000
    assert len(units[1].text) == 1


def test_spreadsheet_document_analysis_default_keeps_candidates_in_one_unit() -> None:
    document = ParsedDocument(
        documentIndex=2,
        fileName="spec.xlsx",
        documentKind="specification",
        text="spreadsheet text",
        textQualityOk=True,
        spreadsheetTables=[{"sheet": "Лист1", "rows": []}],
    )
    positions = [
        TenderPosition(
            candidateId=f"xlsx:spec.xlsx:Лист1:{row}",
            product=f"Товар {row}",
            quantity=1,
            unit="шт",
            source="excel_table_deterministic",
            sourceReference=ProductSourceReference(
                fileName="spec.xlsx",
                sheet="Лист1",
                row=row,
                productColumn="B",
                quantityColumn="D",
                unitColumn="C",
                extractionMethod="excel_deterministic",
            ),
            sourceCells={"B": f"Товар {row}", "C": "шт", "D": "1"},
        )
        for row in range(2, 132)
    ]

    units, warnings = build_document_analysis_units(
        "",
        [document],
        positions,
        _settings(),
    )

    assert not warnings
    assert len(units) == 1
    assert units[0].sourceType == "spreadsheet"
    assert len(units[0].spreadsheetCandidates) == 130
