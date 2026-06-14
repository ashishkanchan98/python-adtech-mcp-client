import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.agent.graph import build_agent_graph, get_llm
from app.agent.tools import create_kb_tool, create_mcp_tools, fetch_mcp_tool_definitions
from app.api.routes import router
from app.config import settings
from app.db.database import init_db


# ── Logging ───────────────────────────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    """Structured JSON logs for Cloud Logging (production)."""
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


if settings.env == "prod":
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logging.root.setLevel(logging.INFO)
    logging.root.handlers = [handler]
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

logger = logging.getLogger(__name__)


# ── API key middleware ────────────────────────────────────────────────────────
class _ApiKeyMiddleware(BaseHTTPMiddleware):
    """Enforces X-API-Key on /api/* when API_KEY is configured."""
    async def dispatch(self, request: Request, call_next):
        if settings.api_key and request.url.path.startswith("/api/"):
            key = request.headers.get("X-API-Key", "")
            if key != settings.api_key:
                raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return await call_next(request)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized  url=%s", settings.database_url)

    tools: list = []
    try:
        tool_defs = await fetch_mcp_tool_definitions(settings.mcp_server_url)
        tools = create_mcp_tools(tool_defs, settings.mcp_server_url)
        logger.info("Loaded %d tools from MCP server at %s", len(tools), settings.mcp_server_url)
    except Exception as exc:
        logger.warning(
            "MCP server unreachable at startup (%s). "
            "Agent will have no tools until the server is available.",
            exc,
        )

    kb_tool = create_kb_tool(settings)
    tools = tools + [kb_tool]
    logger.info("KB tool added  total_tools=%d  kb_provider=%s", len(tools), settings.kb_provider)

    llm = get_llm(settings)
    agent    = build_agent_graph(llm, tools, settings.agent_max_tool_calls)
    agent_hi = build_agent_graph(llm, tools, settings.agent_max_tool_calls, human_interrupt=True)

    app.state.agent    = agent
    app.state.agent_hi = agent_hi
    app.state.tools    = tools

    logger.info(
        "python-adtech-mcp-client ready  env=%s  llm=%s  tools=%d  port=%s",
        settings.env, settings.llm_provider, len(tools), os.environ.get("PORT", settings.port),
    )
    yield

    logger.info("Shutting down python-adtech-mcp-client")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AdTech MCP Client — Python / LangGraph",
    description=(
        "AI support agent built with LangChain + LangGraph. "
        "Discovers and calls tools from adtech-mcp-server via HTTP (MCP pattern)."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # Swagger UI disabled in production — enable only for dev/staging
    docs_url="/docs" if settings.env != "prod" else None,
    redoc_url="/redoc" if settings.env != "prod" else None,
)

# CORS — restrict to actual domain in production via CORS_ORIGINS env var
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API key auth — active only when API_KEY env var is set
if settings.api_key:
    app.add_middleware(_ApiKeyMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(router)
