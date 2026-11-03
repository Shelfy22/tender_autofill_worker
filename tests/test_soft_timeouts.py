from types import SimpleNamespace

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.models import TenderPosition
from app.services.catalog import CatalogMatcher
from app.services.documents import DocumentProcessor
from app.services.product_validation import validate_product_candidates


def timeout(*args, **kwargs):
    raise SoftTimeLimitExceeded()


def test_global_soft_timeout_crosses_document_fallback():
    processor = object.__new__(DocumentProcessor)
    processor.settings = SimpleNamespace(max_documents=10)
    processor.download = timeout
    with pytest.raises(SoftTimeLimitExceeded):
        processor.process_all([{"fileName": "test.docx"}])


def test_global_soft_timeout_is_not_a_missing_catalog_product():
    matcher = object.__new__(CatalogMatcher)
    matcher._match = timeout
    with pytest.raises(SoftTimeLimitExceeded):
        matcher.match_all([TenderPosition(product="Cable", quantity=1)])


def test_global_soft_timeout_crosses_required_product_audit():
    with pytest.raises(SoftTimeLimitExceeded):
        validate_product_candidates(
            SimpleNamespace(audit_product_candidates=timeout),
            [TenderPosition(product="Cable", quantity=1)],
        )
