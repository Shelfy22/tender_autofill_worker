"""Errors that must cross optional catalog/document fallback boundaries."""


class RequiredLlmStageError(RuntimeError):
    """A non-document LLM stage exhausted its configured provider fallbacks."""
