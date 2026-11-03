import pytest


@pytest.fixture
def archived_tender_fields(monkeypatch):
    """Explicit opt-in for preserved business rules, never enabled suite-wide."""
    import app.services.field_policy as policy

    monkeypatch.setattr(policy, "DISABLED_TENDER_FIELDS", frozenset())
