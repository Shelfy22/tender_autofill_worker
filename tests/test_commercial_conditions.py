import pytest

from app.models import ParsedDocument, SpreadsheetRow, SpreadsheetTable


from app.services.commercial_conditions import build_commercial_conditions_context


def document(text: str) -> ParsedDocument:
    return ParsedDocument(documentIndex=1, fileName="Проект договора.docx", text=text)
