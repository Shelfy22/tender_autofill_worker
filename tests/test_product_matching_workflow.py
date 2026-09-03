import json
from pathlib import Path


WORKFLOW_PATH = Path("n8n/Tender Product Matching From Documents.json")


def test_product_matching_workflow_is_importable_and_uses_worker_endpoint() -> None:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    node_names = {node["name"] for node in workflow["nodes"]}
    payload = json.dumps(workflow, ensure_ascii=False)

    assert workflow["active"] is False
    assert "id" not in workflow
    assert "versionId" not in workflow
    assert "Upload tender documents" in node_names
    assert "Build product matching in Tender Worker" in node_names
    assert "/product-matching/from-documents" in payload
    assert "TENDER_PRODUCT_MATCHING_URL" in payload
    assert "TENDER_PYTHON_API_KEY" in payload
    assert "x-product-matching-debug-b64" in payload
    assert "sk-" not in payload