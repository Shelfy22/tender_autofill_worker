from uuid import uuid4

from holding_companies.job_store import HoldingJobStore
from holding_companies.models import HoldingTenderInput, HoldingTenderResult


def test_holding_job_store_keeps_input_private_and_result_retrievable(tmp_path) -> None:
    job_id = str(uuid4())
    store = HoldingJobStore(tmp_path)
    payload = HoldingTenderInput(
        tenderId="2000526719",
        emailText="private e-mail body",
        attachmentText="private attachment text",
        attachmentName="BID2000526719.PDF",
    )
    store.create(job_id, payload)

    state = store.public_state(job_id)
    assert state is not None
    assert state["status"] == "queued"
    assert "emailText" not in state
    assert store.claim(job_id) is not None
    assert store.input(job_id) == payload

    result = HoldingTenderResult(
        tenderId=payload.tenderId,
        fields={"tenderStatus": "\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u043e"},
        productCheck={},
    )
    store.report_path(job_id).write_bytes(b"xlsx")
    store.complete(
        job_id,
        result=result,
        report_file_name="2000526719.xlsx",
        warnings=[],
    )

    assert store.public_state(job_id)["status"] == "completed"
    assert store.result(job_id) == result
