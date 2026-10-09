from app.models import DocumentAnalysisResponse


def test_document_analysis_response_skips_product_rows_without_a_name() -> None:
    response = DocumentAnalysisResponse.model_validate(
        {
            "products": [
                {"positionNumber": None, "quantity": 2},
                {"product": "Трансформатор ТМГФ", "positionNumber": None, "quantity": 1},
            ]
        }
    )

    assert len(response.products) == 1
    assert response.products[0].positionNumber == ""
    assert "Skipped 1 malformed product rows" in response.warnings[0]
