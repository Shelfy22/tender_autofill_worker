import json
from pathlib import Path


FIXTURE = Path(__file__).parents[1] / "evaluation" / "sverka_47" / "expected_overrides.json"


def test_sverka_47_employee_review_is_complete_and_stable() -> None:
    """Keep the reviewed corpus intact when adding new resolver rules."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert len(data["tenderIds"]) == 47
    assert len(set(data["tenderIds"])) == 47
    assert set(data["overrides"]).issubset({str(item) for item in data["tenderIds"]})
    assert sum(len(fields) for fields in data["overrides"].values()) == data["expectedOverrideCount"] == 104
