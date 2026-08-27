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
    max_text_chars_per_file: int = Field(default=250_000, ge=1_000)
    max_combined_text_chars: int = Field(default=500_000, ge=1_000)
    max_product_text_chars: int = Field(default=250_000, ge=1_000)
    max_decision_text_chars: int = Field(default=220_000, ge=1_000)
    pdf_ocr_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1)

    seldon_base_url: str = "https://apitorgi.myseldon.com"
    seldon_username: str | None = None
    seldon_password: SecretStr | None = None

    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: SecretStr | None = None
    # API behaviour must be explicit: production may use an internal OpenRouter proxy
    # whose hostname does not contain "openrouter.ai".
    llm_provider: str = "openrouter"
    llm_model_attempt_1: str = "google/gemini-3.5-flash"
    llm_model_attempt_2: str = "google/gemini-2.5-pro"
    llm_model_attempt_3: str = "openai/gpt-5.5"
    llm_enable_model_fallback: bool = True
    # Optional comma-separated override. Empty means: use the other attempt models.
    llm_fallback_models: str = ""
    llm_timeout_seconds: float = Field(default=300, gt=0)
    llm_max_output_tokens: int = Field(default=16_000, ge=256)
    llm_structured_output_mode: str = "json_schema"
    llm_json_schema_strict: bool = True
    llm_enable_response_healing: bool = True
    llm_require_supported_parameters: bool = True
    ocr_model: str = "google/gemini-2.5-flash"
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

    def models_for_ocr(self) -> list[str]:
        if not self.llm_enable_model_fallback:
            return [self.ocr_model]
        return list(
            dict.fromkeys([self.ocr_model, *self._model_list(self.ocr_fallback_models)])
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
