from holding_companies.prompt import build_extraction_prompt


def test_holding_prompt_joins_email_and_attachment_without_unapproved_reasons() -> None:
    prompt = build_extraction_prompt(
        email_text="Tender 2000526719",
        attachment_name="BID2000526719.PDF",
        attachment_text="Motor 10 pcs",
    )

    assert "Tender 2000526719" in prompt
    assert "BID2000526719.PDF" in prompt
    assert "Motor 10 pcs" in prompt
    assert 'tenderStatus="\u0421\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u0430\u043d\u043e"' in prompt
