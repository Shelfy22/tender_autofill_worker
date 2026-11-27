from holding_companies.models import HoldingTenderExtraction
from holding_companies import policy


def test_empty_reason_directory_prevents_automatic_rejection() -> None:
    extraction = HoldingTenderExtraction(
        tenderStatus="\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e",
        tenderStatusReason="\u041d\u0435\u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d\u043d\u0430\u044f \u043f\u0440\u0438\u0447\u0438\u043d\u0430",
        tenderStatusNote="\u041f\u0440\u0438\u043c\u0435\u0447\u0430\u043d\u0438\u0435",
    )

    decision = policy.resolve_status(extraction, "\u041f\u043e\u043a\u0440\u044b\u0442\u0438\u0435 50%.")

    assert decision.status == "\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u043e"
    assert decision.reason == ""
    assert decision.note == "\u041f\u0440\u0438\u043c\u0435\u0447\u0430\u043d\u0438\u0435 \u041f\u043e\u043a\u0440\u044b\u0442\u0438\u0435 50%."
    assert decision.warnings


def test_approved_reason_can_reject_after_directory_is_configured(monkeypatch) -> None:
    reason = "\u0422\u0435\u0441\u0442\u043e\u0432\u0430\u044f \u043f\u0440\u0438\u0447\u0438\u043d\u0430"
    monkeypatch.setattr(policy, "STATUS_REASONS", frozenset({reason}))
    extraction = HoldingTenderExtraction(
        tenderStatus="\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e",
        tenderStatusReason=reason,
    )

    decision = policy.resolve_status(extraction, "")

    assert decision.status == "\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e"
    assert decision.reason == reason
    assert not decision.warnings


def test_delivery_deadline_rejection_requires_more_than_eighteen_months() -> None:
    extraction = HoldingTenderExtraction(
        tenderStatus="\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e",
        tenderStatusReason=policy.DELIVERY_DEADLINE_REASON,
        deliveryEndDate="2028-06-02",
    )

    decision = policy.resolve_status(extraction, "", received_at="2026-11-01T12:00:00+03:00")

    assert decision.status == "\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e"
    assert decision.reason == policy.DELIVERY_DEADLINE_REASON


def test_indivisible_lot_rejection_requires_explicit_indivisibility_and_low_coverage() -> None:
    extraction = HoldingTenderExtraction(lotDivisible=False)

    decision = policy.resolve_status(
        extraction,
        "\u041f\u043e\u043a\u0440\u044b\u0442\u0438\u0435 75%.",
        product_check={"coveragePercent": 75.0},
    )

    assert decision.status == "\u041e\u0442\u043a\u0430\u0437\u0430\u043d\u043e"
    assert decision.reason == policy.INDIVISIBLE_LOT_REASON
