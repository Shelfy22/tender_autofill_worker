from app.config import Settings
from app.models import (
    DocumentAnalysisResult,
    ParsedDocument,
    ProductSourceReference,
    TenderPosition,
)
from app.services.document_analysis import (
    build_document_analysis_units,
    compact_document_analysis_results,
    fallback_document_consolidation,
)


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


def test_document_analysis_batches_two_small_documents_by_default() -> None:
    documents = [
        ParsedDocument(
            documentIndex=1,
            fileName="doc-1.pdf",
            documentKind="technical",
            text="document 1",
            textQualityOk=True,
        ),
        ParsedDocument(
            documentIndex=2,
            fileName="doc-2.docx",
            documentKind="contract",
            text="document 2",
            textQualityOk=True,
        ),
        ParsedDocument(
            documentIndex=3,
            fileName="doc-3.txt",
            documentKind="technical",
            text="document 3",
            textQualityOk=True,
        ),
    ]

    units, warnings = build_document_analysis_units(
        "",
        documents,
        [],
        _settings(),
    )

    assert not warnings
    assert len(units) == 2
    assert units[0].unitId == "unit:1:documents:1-2"
    assert units[0].documentIndex is None
    assert units[0].fileName == "doc-1.pdf; doc-2.docx"
    assert units[0].documentKind == "document_batch"
    assert "--- DOCUMENT 1 ---" in units[0].text
    assert "--- DOCUMENT 2 ---" in units[0].text
    assert units[1].documentIndex == 3
    assert units[0].batchedDocumentUnits
    assert [child.fileName for child in units[0].batchedDocumentUnits] == ["doc-1.pdf", "doc-2.docx"]
    assert "batchedDocumentUnits" not in units[0].model_dump(mode="json")
    assert units[1].text == "document 3"



def test_document_analysis_keeps_spreadsheet_text_fallback_out_of_document_batches() -> None:
    documents = [
        ParsedDocument(
            documentIndex=1,
            fileName="terms.docx",
            documentKind="contract",
            text="terms",
            textQualityOk=True,
        ),
        ParsedDocument(
            documentIndex=2,
            fileName="spec.xlsx",
            fileExtension=".xlsx",
            documentKind="specification",
            text="spreadsheet text fallback",
            textQualityOk=True,
        ),
        ParsedDocument(
            documentIndex=3,
            fileName="more.pdf",
            documentKind="technical",
            text="more terms",
            textQualityOk=True,
        ),
    ]

    units, warnings = build_document_analysis_units(
        "",
        documents,
        [],
        _settings(),
        skip_spreadsheet_candidate_units=True,
    )

    assert not warnings
    assert len(units) == 3
    assert [unit.documentIndex for unit in units] == [1, 2, 3]
    assert all(";" not in unit.fileName for unit in units)
    assert all(not unit.batchedDocumentUnits for unit in units)


def test_document_analysis_does_not_batch_documents_above_unit_limit() -> None:
    documents = [
        ParsedDocument(
            documentIndex=index,
            fileName=f"large-{index}.txt",
            documentKind="technical",
            text="A" * 6_000,
            textQualityOk=True,
        )
        for index in range(1, 3)
    ]

    units, warnings = build_document_analysis_units(
        "",
        documents,
        [],
        _settings(document_analysis_unit_max_chars=10_000),
    )

    assert not warnings
    assert len(units) == 2
    assert [unit.documentIndex for unit in units] == [1, 2]
    assert [len(unit.text) for unit in units] == [6_000, 6_000]


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


def test_document_analysis_splits_spreadsheet_text_fallback_at_100k_chars() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName='large-spec.xlsx',
        fileExtension='.xlsx',
        documentKind='specification',
        text='A' * 250_001,
        textQualityOk=True,
    )

    units, warnings = build_document_analysis_units(
        '',
        [document],
        [],
        _settings(),
        skip_spreadsheet_candidate_units=True,
    )

    assert not warnings
    assert [unit.partIndex for unit in units] == [1, 2, 3]
    assert [unit.partTotal for unit in units] == [3, 3, 3]
    assert [len(unit.text) for unit in units] == [100_000, 100_000, 50_001]


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


def test_document_analysis_products_are_deduplicated_across_documents() -> None:
    results = [
        DocumentAnalysisResult(
            unitId="unit-1",
            fileName="spec.xlsx",
            products=[
                TenderPosition(
                    product="Torque wrench",
                    quantity=2,
                    unit="pcs",
                    evidence="specification",
                )
            ],
        ),
        DocumentAnalysisResult(
            unitId="unit-2",
            fileName="price.xlsx",
            products=[
                TenderPosition(
                    product="Torque wrench",
                    article="TW-1",
                    quantity=2,
                    unit="pcs",
                ),
                TenderPosition(product="Torque wrench", quantity=3, unit="pcs"),
                TenderPosition(product="Pressure gauge", quantity=1, unit="pcs"),
            ],
        ),
    ]

    compact, debug = compact_document_analysis_results(results)

    assert debug == {
        "inputProductCount": 4,
        "uniqueProductCount": 3,
        "removedDuplicateCount": 1,
    }
    assert [len(result["products"]) for result in compact] == [1, 2]
    assert compact[0]["products"][0]["article"] == "TW-1"


def test_large_spreadsheet_is_skipped_by_document_analysis() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName="large.xlsx",
        text="large spreadsheet fallback text",
        textQualityOk=True,
        spreadsheetTables=[{"sheet": "Лист1", "rows": []}],
    )
    positions = [
        TenderPosition(
            candidateId=f"xlsx:large.xlsx:Лист1:{row}",
            product=f"Товар {row}",
            productQuery=f"Товар {row}",
            sourceReference=ProductSourceReference(
                fileName="large.xlsx",
                sheet="Лист1",
                row=row,
                productColumn="B",
                extractionMethod="excel_deterministic",
            ),
        )
        for row in range(2, 7)
    ]

    units, warnings = build_document_analysis_units(
        "",
        [document],
        positions,
        _settings(spreadsheet_llm_max_positions=4),
        skip_spreadsheet_candidate_units=True,
    )

    assert units == []
    assert any("5 deterministic positions exceed LLM limit 4" in item for item in warnings)


def test_fallback_document_consolidation_preserves_structured_results() -> None:
    results = [
        DocumentAnalysisResult(
            unitId="unit-1",
            products=[TenderPosition(product="Кабель", productQuery="Кабель")],
            analysisIncomplete=True,
            warnings=["unit timeout"],
        ).model_dump(mode="json")
    ]

    consolidation = fallback_document_consolidation(
        results,
        "LLM consolidation timeout",
    )

    assert [item.product for item in consolidation.products] == ["Кабель"]
    assert consolidation.incompleteUnitIds == ["unit-1"]
    assert "LLM consolidation timeout" in consolidation.warnings
    assert "unit timeout" in consolidation.warnings
