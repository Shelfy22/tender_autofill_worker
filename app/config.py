from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: str = "production"
    log_level: str = "INFO"
    api_key: SecretStr | None = None
    api_max_batch_jobs: int = Field(default=25, ge=1, le=500)

    postgres_dsn: SecretStr
    postgres_pool_min_size: int = Field(default=1, ge=1)
    postgres_pool_max_size: int = Field(default=10, ge=1)
    postgres_connect_timeout_seconds: int = Field(default=10, ge=1)
    observability_enabled: bool = True
    observability_heartbeat_seconds: int = Field(default=30, ge=5, le=300)

    redis_url: SecretStr = SecretStr("redis://tender-redis:6379/0")
    celery_queue: str = "tender-autofill"
    # Must remain below n8n Controller stale-processing threshold (30 minutes).
    celery_soft_time_limit_seconds: int = Field(default=1500, ge=60)
    celery_hard_time_limit_seconds: int = Field(default=1680, ge=60)

    temp_root: Path = Path("/tmp/tender-autofill")
    http_connect_timeout_seconds: float = Field(default=15, gt=0)
    http_read_timeout_seconds: float = Field(default=120, gt=0)
    document_download_timeout_seconds: float = Field(default=180, gt=0)
    # Normal verification is always attempted first. This fallback applies only
    # to procurement document downloads, never to Seldon/LLM/database traffic.
    document_allow_insecure_ssl_fallback: bool = True
    document_enable_curl_fallback: bool = True
    curl_binary: str = "curl"
    conversion_timeout_seconds: int = Field(default=120, ge=10)

    max_documents: int = Field(default=100, ge=1)
    max_download_bytes_per_file: int = Field(default=100 * 1024 * 1024, ge=1)
    max_download_bytes_total: int = Field(default=500 * 1024 * 1024, ge=1)
    max_archive_members: int = Field(default=500, ge=1)
    max_archive_uncompressed_bytes: int = Field(default=500 * 1024 * 1024, ge=1)
    max_archive_compression_ratio: float = Field(default=100.0, ge=1)
    max_archive_depth: int = Field(default=1, ge=0, le=5)
    max_text_chars_per_file: int = Field(default=1_000_000, ge=1_000)
    max_combined_text_chars: int = Field(default=1_000_000, ge=1_000)
    max_product_text_chars: int = Field(default=1_000_000, ge=1_000)
    max_decision_text_chars: int = Field(default=220_000, ge=1_000)
    enable_document_analysis_pipeline: bool = True
    document_analysis_unit_max_chars: int = Field(default=1_000_000, ge=5_000)
    document_analysis_max_units: int = Field(default=100, ge=1, le=500)
    spreadsheet_candidate_review_max_rows: int = Field(default=100_000, ge=5, le=1_000_000)
    spreadsheet_candidate_review_max_chars: int = Field(default=1_000_000, ge=5_000)
    pdf_ocr_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1)

    seldon_base_url: str = "https://apitorgi.myseldon.com"
    seldon_username: str | None = None
    seldon_password: SecretStr | None = None

    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = None
    # API behaviour must be explicit: production may use an internal OpenRouter proxy
    # whose hostname does not contain "openrouter.ai".
    llm_provider: str = "openrouter"
    llm_model_attempt_1: str = "deepseek/deepseek-v4-flash-0731"
    llm_model_attempt_2: str = "qwen/qwen3.7-flash"
    llm_model_attempt_3: str = "qwen/qwen3.5-flash-02-23"
    llm_enable_model_fallback: bool = True
    # Optional comma-separated override. Empty means: use the other attempt models.
    llm_fallback_models: str = ""
    llm_timeout_seconds: float = Field(default=300, gt=0)
    llm_max_attempts_per_unit: int = Field(default=2, ge=1, le=3)
    # Optional per-stage HTTP timeouts. None means use llm_timeout_seconds from the client.
    document_analysis_timeout_seconds: float | None = Field(default=120, gt=0)
    product_extraction_timeout_seconds: float | None = Field(default=90, gt=0)
    catalog_selection_timeout_seconds: float | None = Field(default=45, gt=0)
    final_decision_timeout_seconds: float | None = Field(default=60, gt=0)
    ocr_timeout_seconds: float | None = Field(default=180, gt=0)
    # Legacy global cap kept for backward compatibility with existing .env files.
    llm_max_output_tokens: int = Field(default=16_000, ge=256)
    # New per-stage completion-token budgets. LLM_MAX_COMPLETION_TOKENS is the
    # default for structured JSON calls; stage-specific values override it only
    # where a stage genuinely needs a different output budget.
    llm_max_completion_tokens: int | None = Field(default=24_000, ge=256)
    document_analysis_max_completion_tokens: int | None = Field(default=24_000, ge=256)
    product_extraction_max_completion_tokens: int | None = Field(default=24_000, ge=256)
    catalog_selection_max_completion_tokens: int | None = Field(default=4_000, ge=256)
    final_decision_max_completion_tokens: int | None = Field(default=4_000, ge=256)
    ocr_max_completion_tokens: int | None = Field(default=24_000, ge=256)
    llm_structured_output_mode: str = "json_schema"
    llm_json_schema_strict: bool = True
    llm_enable_response_healing: bool = True
    llm_require_supported_parameters: bool = True
    # OpenRouter reasoning/thinking tokens are billed as completion tokens. Keep
    # structured JSON calls in non-thinking mode unless explicitly overridden.
    llm_reasoning_effort: str = "none"
    catalog_selection_model: str = "openai/gpt-oss-120b"
    catalog_selection_fallback_models: str = "qwen/qwen3.7-flash,qwen/qwen3.5-flash-02-23"
    ocr_model: str = "deepseek/deepseek-v4-flash-0731"
    ocr_fallback_models: str = ""
    ocr_pdf_engine: str = "mistral-ocr"

    ipro_base_url: str = "https://idev.etm.ru/api/ipro/user/registration_ipro"
    ipro_token: SecretStr | None = None

    catalog_mode: str = "disabled"
    catalog_search_url: str | None = None
    catalog_api_key: SecretStr | None = None
    catalog_timeout_seconds: float = Field(default=120, gt=0)
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_collection: str = "products"
    qdrant_vector_name: str | None = None
    qdrant_top_k: int = Field(default=50, ge=1, le=500)
    ollama_url: str | None = None
    ollama_embedding_model: str = "qwen3-embedder-ft:latest"

    enable_tender_html_fetch: bool = False
    libreoffice_binary: str = "libreoffice"
    seven_zip_binary: str = "7z"
    unar_binary: str = "unar"
    lsar_binary: str = "lsar"
    bsdtar_binary: str = "bsdtar"

    @field_validator("catalog_mode")
    @classmethod
    def validate_catalog_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "http", "qdrant"}:
            raise ValueError("catalog_mode must be disabled, http, or qdrant")
        return normalized

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"openrouter", "openai_compatible"}:
            raise ValueError("llm_provider must be openrouter or openai_compatible")
        return normalized

    @field_validator("llm_structured_output_mode")
    @classmethod
    def validate_llm_structured_output_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"json_schema", "json_object"}:
            raise ValueError(
                "llm_structured_output_mode must be json_schema or json_object"
            )
        return normalized

    @field_validator("llm_reasoning_effort")
    @classmethod
    def validate_llm_reasoning_effort(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        if normalized not in allowed:
            raise ValueError(
                "llm_reasoning_effort must be none, minimal, low, medium, high, xhigh, or max"
            )
        return normalized

    def model_for_attempt(self, attempt: int) -> str:
        if attempt <= 1:
            return self.llm_model_attempt_1
        if attempt == 2:
            return self.llm_model_attempt_2
        return self.llm_model_attempt_3

    @staticmethod
    def _model_list(value: str) -> list[str]:
        return [model.strip() for model in value.split(",") if model.strip()]

    def models_for_attempt(self, attempt: int) -> list[str]:
        primary = self.model_for_attempt(attempt)
        if not self.llm_enable_model_fallback:
            return [primary]
        configured = self._model_list(self.llm_fallback_models)
        attempt_models = [
            self.llm_model_attempt_1,
            self.llm_model_attempt_2,
            self.llm_model_attempt_3,
        ]
        index = min(max(attempt, 1), 3) - 1
        candidates = configured or [*attempt_models[index:], *attempt_models[:index]]
        return list(dict.fromkeys([primary, *candidates]))

    def models_for_catalog_selection(self) -> list[str]:
        if not self.llm_enable_model_fallback:
            return [self.catalog_selection_model]
        return list(
            dict.fromkeys(
                [
                    self.catalog_selection_model,
                    *self._model_list(self.catalog_selection_fallback_models),
                ]
            )
        )

    def models_for_ocr(self) -> list[str]:
        if not self.llm_enable_model_fallback:
            return [self.ocr_model]
        return list(
            dict.fromkeys([self.ocr_model, *self._model_list(self.ocr_fallback_models)])
        )

    def max_completion_tokens_for(self, operation: str) -> int:
        """Return the configured completion-token budget for an LLM stage.

        llm_max_output_tokens remains a backward-compatible fallback for old
        deployments. New deployments should prefer LLM_MAX_COMPLETION_TOKENS and
        the stage-specific overrides below.
        """
        base = self.llm_max_completion_tokens or self.llm_max_output_tokens
        normalized = (operation or "").strip().lower()
        if normalized in {"ocr_pdf", "ocr"}:
            return self.ocr_max_completion_tokens or base
        if normalized in {"catalog_product_selection", "select_catalog_product"}:
            return self.catalog_selection_max_completion_tokens or base
        if normalized in {"final_decision", "apply_final_decision", "decide_tender_status"}:
            return self.final_decision_max_completion_tokens or base
        if normalized in {"extract_tender_products", "audit_product_candidates"}:
            return self.product_extraction_max_completion_tokens or base
        if normalized.startswith("analyze_document"):
            return self.document_analysis_max_completion_tokens or base
        return base

    def timeout_for(self, operation: str) -> float | None:
        normalized = (operation or "").strip().lower()
        if normalized in {"ocr_pdf", "ocr"}:
            return self.ocr_timeout_seconds
        if normalized in {"catalog_product_selection", "select_catalog_product"}:
            return self.catalog_selection_timeout_seconds
        if normalized in {"final_decision", "apply_final_decision", "decide_tender_status"}:
            return self.final_decision_timeout_seconds
        if normalized in {"extract_tender_products", "audit_product_candidates"}:
            return self.product_extraction_timeout_seconds
        if normalized.startswith("analyze_document"):
            return self.document_analysis_timeout_seconds
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
