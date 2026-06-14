from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Environment ───────────────────────────────────────────────────────────
    # "dev"  → SQLite, ChromaDB, plain text logs, Swagger UI enabled
    # "prod" → Cloud SQL, Vertex AI Search, JSON logs, Swagger UI disabled
    env: Literal["dev", "prod"] = "dev"

    # Server
    port: int = 8080

    # MCP Server — defaults to python-adtech-mcp-server (port 8082)
    # Set MCP_SERVER_URL=http://localhost:8081 to use the Java server instead
    mcp_server_url: str = "http://localhost:8082"

    # ── LLM provider ─────────────────────────────────────────────────────────
    # "groq"   → free tier, dev/staging
    # "vertex" → Gemini 2.0 Flash, production (uses ADC — no key needed)
    llm_provider: Literal["groq", "vertex"] = "groq"

    # Groq
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Vertex AI
    vertex_project_id: str = ""
    vertex_location: str = "us-central1"
    vertex_model: str = "gemini-2.0-flash-001"

    # ── Agent ─────────────────────────────────────────────────────────────────
    agent_max_tool_calls: int = 6

    # ── Database ──────────────────────────────────────────────────────────────
    # dev  → SQLite (aiosqlite)
    # prod → postgresql+asyncpg://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
    database_url: str = "sqlite+aiosqlite:///./tickets.db"

    # ── Knowledge Base provider ───────────────────────────────────────────────
    # "local" → ChromaDB (embedded, seeded from kb-docs/ — default for dev)
    # "gcp"   → Vertex AI Search (requires GCP_PROJECT_ID — production)
    kb_provider: Literal["local", "gcp"] = "local"

    # Local KB (ChromaDB)
    kb_docs_dir: str = "./kb-docs"
    chroma_db_path: str = "./chroma_db"

    # GCP KB (Vertex AI Search)
    gcp_project_id: str = ""
    kb_datastore_id: str = "adtech-kb"
    kb_serving_config: str = "default_search"

    # ── Security ──────────────────────────────────────────────────────────────
    # API key for /api/* endpoints. Empty = no auth (dev mode).
    # Production: store in Secret Manager, inject as API_KEY env var.
    api_key: str = ""

    # CORS allowed origins. "*" = allow all (dev only).
    # Production: set to your actual frontend domain.
    # e.g. "https://adtech-support.yourdomain.com"
    cors_origins: str = "*"


settings = Settings()
