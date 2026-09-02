from pathlib import Path

import httpx

from app.config import Settings
from app.services.customer import IProClient


def test_ipro_data_rows_contract_and_url(tmp_path: Path) -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "status": {"code": 200},
                "data": {
                    "rows": [
                        {
                            "innOrg": "1234567890",
                            "kppOrg": "123456789",
                            "fullNameOrg": "ООО Тест",
                        }
                    ]
                },
            },
        )

    client = IProClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            temp_root=tmp_path,
            ipro_base_url="https://example.test/api/ipro/user/registration_ipro",
        )
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://example.test/api/ipro/user/registration_ipro/",
        transport=httpx.MockTransport(handler),
    )
    fields, _, lookup, warnings = client.lookup(
        {"counterpartyInn": "1234567890", "counterpartyKpp": "123456789"}, {}
    )
    client.close()

    assert captured == [
        "https://example.test/api/ipro/user/registration_ipro/orgByBir?inn=1234567890"
    ]
    assert lookup["status"] == "matched"
    assert fields["counterpartyName"] == "ООО Тест"
    assert not warnings


def test_ipro_approval_uses_inn_only_and_ignores_kpp_mismatch(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": {"code": 200},
                "data": {
                    "rows": [
                        {
                            "idOrg": 42,
                            "innOrg": "1234567890",
                            "kppOrg": "999999999",
                            "fullNameOrg": "ООО Совпадение по ИНН",
                        }
                    ]
                },
            },
        )

    client = IProClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            temp_root=tmp_path,
            ipro_base_url="https://example.test/api/ipro/user/registration_ipro",
        )
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://example.test/api/ipro/user/registration_ipro/",
        transport=httpx.MockTransport(handler),
    )
    fields, _, lookup, warnings = client.lookup(
        {"counterpartyInn": "1234567890", "counterpartyKpp": "123456789"}, {}
    )
    client.close()

    assert lookup["status"] == "matched"
    assert lookup["matchType"] == "inn"
    assert fields["counterpartyName"] == "ООО Совпадение по ИНН"
    assert fields["counterpartyKpp"] == "123456789"
    assert not warnings


def test_ipro_multiple_rows_with_same_inn_still_passes(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": {"code": 200},
                "data": {
                    "rows": [
                        {"innOrg": "1234567890", "kppOrg": "111111111"},
                        {"innOrg": "1234567890", "kppOrg": "222222222"},
                    ]
                },
            },
        )

    client = IProClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            temp_root=tmp_path,
            ipro_base_url="https://example.test/api/ipro/user/registration_ipro",
        )
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://example.test/api/ipro/user/registration_ipro/",
        transport=httpx.MockTransport(handler),
    )
    _, _, lookup, warnings = client.lookup({"counterpartyInn": "1234567890"}, {})
    client.close()

    assert lookup["status"] == "matched"
    assert lookup["matchType"] == "inn"
    assert not warnings


def test_ipro_not_found_keeps_response_summary_for_diagnostics(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": {"code": 200},
                "data": {
                    "rows": [
                        {
                            "innOrg": "7700000000",
                            "kppOrg": "770001001",
                            "fullNameOrg": "ООО Другая организация",
                        }
                    ]
                },
            },
        )

    client = IProClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            temp_root=tmp_path,
            ipro_base_url="https://example.test/api/ipro/user/registration_ipro",
        )
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://example.test/api/ipro/user/registration_ipro/",
        transport=httpx.MockTransport(handler),
    )
    _, _, lookup, warnings = client.lookup({"counterpartyInn": "1234567890"}, {})
    client.close()

    assert lookup["status"] == "not_found"
    assert lookup["requestedInn"] == "1234567890"
    assert lookup["httpStatus"] == 200
    assert lookup["apiCode"] == 200
    assert lookup["apiRowsCount"] == 1
    assert lookup["byInnCount"] == 0
    assert lookup["candidateRows"] == [
        {
            "inn": "7700000000",
            "kpp": "770001001",
            "fullName": "ООО Другая организация",
            "shortName": None,
        }
    ]
    assert warnings
