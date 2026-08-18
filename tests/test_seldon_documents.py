from types import SimpleNamespace

import pytest

from app.models import NormalizedJob
from app.services.seldon import SeldonClient, SeldonDocumentsError


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeHttp:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def post(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(self.payload)


def client_with_response(payload: dict[str, object]) -> SeldonClient:
    client = object.__new__(SeldonClient)
    client.settings = SimpleNamespace(seldon_base_url="https://seldon.test")
    client.http = FakeHttp(payload)
    return client


def test_explicit_seldon_404_is_business_missing_documentation_state() -> None:
    client = client_with_response(
        {
            "status": {
                "code": 404,
                "descr": "По запрошенному идентификатору закупки отсутствует документация",
            }
        }
    )
    job = NormalizedJob(
        job_record_key="r",
        batch_id="b",
        report_id=3,
        seldon_id="1091225514",
    )

    result = client.get_purchase_documents(job, "token")

    assert result.documentation_missing is True
    assert result.api_code == 404
    assert result.documents == []
    assert "Seldon не выдал документы после запроса" in result.decision_context()[
        "documentationNote"
    ]


def test_seldon_server_failure_is_technical_error_for_controller_retry() -> None:
    client = client_with_response(
        {"status": {"code": 500, "descr": "temporary upstream failure"}}
    )
    job = NormalizedJob(
        job_record_key="r",
        batch_id="b",
        report_id=3,
        seldon_id="1091225514",
    )

    with pytest.raises(SeldonDocumentsError, match="Техническая ошибка Seldon"):
        client.get_purchase_documents(job, "token")
