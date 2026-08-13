from __future__ import annotations

import json
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "reference"
    / "Daily Tender Controller + Finalizer.json"
)


def load_workflow() -> dict[str, object]:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def node_by_name(workflow: dict[str, object], name: str) -> dict[str, object]:
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    return next(node for node in nodes if isinstance(node, dict) and node.get("name") == name)


def first_target(workflow: dict[str, object], source: str) -> str:
    connections = workflow["connections"]
    assert isinstance(connections, dict)
    return connections[source]["main"][0][0]["node"]


def test_controller_import_is_safe_and_initially_limited_to_one() -> None:
    workflow = load_workflow()

    assert workflow["active"] is False
    assert "id" not in workflow
    assert "versionId" not in workflow

    claim = node_by_name(workflow, "Postgres — занять 1 слот (первый тест)")
    assert claim["parameters"]["options"]["queryReplacement"] == "={{ [1] }}"


def test_execute_subworkflow_is_replaced_by_verified_http_batch() -> None:
    workflow = load_workflow()
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    assert all(node.get("type") != "n8n-nodes-base.executeWorkflow" for node in nodes)

    assert first_target(workflow, "Подготовить jobs для Tender Worker") == (
        "Собрать HTTP batch для Python"
    )
    assert first_target(workflow, "Собрать HTTP batch для Python") == (
        "HTTP — dispatch Tender jobs в Python"
    )
    assert first_target(workflow, "HTTP — dispatch Tender jobs в Python") == (
        "Проверить ответ Python API"
    )

    http_node = node_by_name(workflow, "HTTP — dispatch Tender jobs в Python")
    parameters = http_node["parameters"]
    assert parameters["method"] == "POST"
    assert parameters["url"] == "http://tender-api:8000/jobs/batch"
    assert "TENDER_PYTHON_API_KEY" in json.dumps(parameters, ensure_ascii=False)
    assert "sk-" not in json.dumps(parameters, ensure_ascii=False)


def test_existing_retry_and_finalizer_are_preserved() -> None:
    workflow = load_workflow()
    names = {node["name"] for node in workflow["nodes"]}

    assert "Postgres — повторить failed до 3 попыток" in names
    assert "Postgres — захватить готовый batch" in names
    assert "Postgres — получить jobs batch" in names
    assert "Собрать Daily Batch Summary из PostgreSQL" in names
    assert "Сформировать CSV по типам тендеров" in names
    assert "Создать CSV binary" in names
    assert "Postgres — batch finished" in names
