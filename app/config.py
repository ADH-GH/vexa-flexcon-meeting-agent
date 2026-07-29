"""Configuration. Secrets + bootstrap come from env; operational knobs live in the DB `settings`
table (dashboard-editable) and are layered on top at runtime."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- storage ---
    database_url: str = "postgresql+psycopg://vfma:vfma@postgres:5432/vfma"

    # --- Vexa ---
    vexa_api_url: str = "http://vexa-gateway-001:8000"
    vexa_api_key: str = ""  # X-API-Key

    # --- diarizer service (github.com/ADH-GH/diarizer) ---
    diarizer_url: str = "http://vexa-diarization-001:8008"

    # --- LLM (OpenAI-compatible) ---
    llm_base_url: str = ""            # e.g. https://litellm.kraemer-ki.de/v1
    llm_api_key: str = ""
    llm_model: str = "DeepSeek-V4-Flash"

    # --- mail ---
    mail_transport: str = "smtp"      # "smtp" | "graph"
    mail_from: str = "automation@flexcon-it.de"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

    # --- MS Graph (calendar for agent-dispatch; optional mail transport) ---
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_calendar_user: str = "automation@flexcon-it.de"

    # --- auth (Entra SSO primary + local user/password fallback) ---
    entra_sso_enabled: bool = True
    admin_user: str = "admin"
    admin_password: str = ""          # local fallback login; set in .env
    session_secret: str = "change-me"
    token_encryption_key: str = ""    # Fernet key for user refresh tokens at rest (set in prod)

    # --- bootstrap tenant (single-tenant testing continuity before onboarding lands) ---
    bootstrap_tenant_entra_id: str = "flexcon-local"
    bootstrap_tenant_name: str = "Flexcon (local)"

    # --- scheduler cadence (seconds) ---
    poll_postcall_s: int = 600
    poll_agent_s: int = 120

    internal_domain: str = "@flexcon-it.de"


settings = Settings()
