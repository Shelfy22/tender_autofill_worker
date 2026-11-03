from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.models import DocumentAnalysisResponse, DocumentAnalysisUnit, TenderConsolidationResponse
from app.services.analysis_pipeline import analyze_document_units, consolidate_analysis
from app.services.llm import LlmWallTimeoutError


def unit(name):
    return DocumentAnalysisUnit(unitId=name, sourceType="document", fileName=name, text=name)


def test_document_timeout_keeps_other_documents_and_records_incomplete_unit():
    def analyze(item):
        if item.unitId == "broken":
            raise LlmWallTimeoutError("provider timeout")
        return DocumentAnalysisResponse()

    warnings = []
    results, incomplete = analyze_document_units(
        SimpleNamespace(analyze_document_unit=analyze), [unit("broken"), unit("good")], warnings,
    )
    assert [result.unitId for result in results] == ["broken", "good"]
    assert results[0].analysisIncomplete is True
    assert results[1].analysisIncomplete is False
    assert incomplete == ["broken"]
    assert warnings


def test_global_soft_timeout_is_never_a_partial_document():
    def analyze(item):
        raise SoftTimeLimitExceeded()

    with pytest.raises(SoftTimeLimitExceeded):
        analyze_document_units(SimpleNamespace(analyze_document_unit=analyze), [unit("one")], [])


def test_consolidation_failure_stops_processing():
    def consolidate(results):
        raise LlmWallTimeoutError("consolidation timeout")

    with pytest.raises(LlmWallTimeoutError):
        consolidate_analysis(SimpleNamespace(consolidate_document_analysis=consolidate), [], [], [])


def test_successful_consolidation_preserves_incomplete_document_metadata():
    llm = SimpleNamespace(consolidate_document_analysis=lambda results: TenderConsolidationResponse())
    consolidated, debug, _ = consolidate_analysis(llm, [], ["broken"], [])
    assert consolidated.incompleteUnitIds == ["broken"]
    assert debug["inputProductCount"] == 0
