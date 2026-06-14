import json
import logging
from typing import Any, Optional

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[str, type] = {
    "string":  str,
    "integer": int,
    "number":  float,
    "boolean": bool,
    "array":   list,
    "object":  dict,
}


def _build_input_model(tool_name: str, schema: dict) -> type[BaseModel]:
    """Convert a JSON Schema object into a Pydantic model for LangChain."""
    properties: dict = schema.get("properties", {})
    required_fields: set = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    for field_name, field_schema in properties.items():
        py_type = _TYPE_MAP.get(field_schema.get("type", "string"), str)
        desc = field_schema.get("description", "")

        if field_name in required_fields:
            fields[field_name] = (py_type, Field(description=desc))
        else:
            default = field_schema.get("default", None)
            fields[field_name] = (Optional[py_type], Field(default=default, description=desc))

    return create_model(f"{tool_name}Input", **fields)


def _make_sync_fn(tool_name: str, mcp_server_url: str):
    """Sync version — used as fallback when async is not available."""
    def _fn(**kwargs: Any) -> str:
        clean = {k: v for k, v in kwargs.items() if v is not None}
        logger.info("MCP tool call (sync): %s  args=%s", tool_name, clean)
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"{mcp_server_url}/mcp/tools/{tool_name}", json=clean)
            resp.raise_for_status()
            return json.dumps(resp.json())
    _fn.__name__ = tool_name
    return _fn


def _make_async_fn(tool_name: str, mcp_server_url: str):
    """Async version — used by LangGraph ToolNode for parallel execution."""
    async def _fn(**kwargs: Any) -> str:
        clean = {k: v for k, v in kwargs.items() if v is not None}
        logger.info("MCP tool call (async): %s  args=%s", tool_name, clean)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{mcp_server_url}/mcp/tools/{tool_name}", json=clean)
            resp.raise_for_status()
            result = resp.json()
            logger.info("MCP tool %s  returned %d keys", tool_name, len(result))
            return json.dumps(result)
    _fn.__name__ = tool_name
    return _fn


def create_mcp_tools(tool_definitions: list[dict], mcp_server_url: str) -> list[StructuredTool]:
    """
    Convert raw MCP server tool definitions to LangChain StructuredTools.
    Each tool POSTs to adtech-mcp-server POST /mcp/tools/{toolName}.
    LangGraph ToolNode calls the coroutine for parallel async execution.
    """
    tools: list[StructuredTool] = []

    for tool_def in tool_definitions:
        name: str = tool_def["name"]
        description: str = tool_def["description"]
        schema: dict = json.loads(tool_def["parametersSchema"])
        input_model = _build_input_model(name, schema)

        tools.append(StructuredTool(
            name=name,
            description=description,
            args_schema=input_model,
            func=_make_sync_fn(name, mcp_server_url),
            coroutine=_make_async_fn(name, mcp_server_url),
        ))

    logger.info("Built %d LangChain tools from MCP server definitions", len(tools))
    return tools


async def fetch_mcp_tool_definitions(mcp_server_url: str) -> list[dict]:
    """GET {mcp_server_url}/mcp/tools — returns raw tool definition list."""
    logger.info("Fetching tool definitions from %s/mcp/tools", mcp_server_url)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{mcp_server_url}/mcp/tools")
        resp.raise_for_status()
        definitions = resp.json()
    logger.info("Received %d tool definitions from MCP server", len(definitions))
    return definitions


# ── Knowledge Base ────────────────────────────────────────────────────────────
# Native LangChain tool — AI capability lives here, not on the MCP server.
# KB_PROVIDER=local  → ChromaDB (embedded, seeded from kb-docs/, no GCP needed)
# KB_PROVIDER=gcp    → Vertex AI Search (Discovery Engine, requires GCP_PROJECT_ID)

import asyncio
from pathlib import Path

# Singleton ChromaDB collection — initialised once per process
_chroma_collection = None


def _get_chroma_collection(settings):
    """Lazy-init ChromaDB collection. Seeds from kb-docs/ on first call."""
    global _chroma_collection
    if _chroma_collection is not None:
        return _chroma_collection

    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    # all-MiniLM-L6-v2 is ~90 MB, downloaded once to ~/.cache/huggingface/
    ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path=settings.chroma_db_path)

    collection = client.get_or_create_collection("adtech_kb", embedding_function=ef)

    if collection.count() == 0:
        _seed_chroma(collection, settings.kb_docs_dir)

    logger.info("ChromaDB ready  docs=%d  path=%s", collection.count(), settings.chroma_db_path)
    _chroma_collection = collection
    return collection


def _seed_chroma(collection, kb_docs_dir: str) -> None:
    """Load all kb-docs/*.json into the ChromaDB collection."""
    docs_path = Path(kb_docs_dir)
    if not docs_path.exists():
        raise FileNotFoundError(
            f"kb-docs not found at '{kb_docs_dir}'. "
            "Run from the python-adtech-mcp-client directory or set KB_DOCS_DIR."
        )

    json_files = sorted(docs_path.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"No JSON files found in {kb_docs_dir}")

    ids, documents, metadatas = [], [], []
    for f in json_files:
        with open(f) as fp:
            doc = json.load(fp)
        struct = doc["structData"]
        title = struct.get("title", "")
        content = struct.get("content", "")
        ids.append(doc["id"])
        documents.append(f"{title}\n\n{content}")
        metadatas.append({
            "title": title,
            "category": struct.get("category", ""),
            "link": struct.get("link", ""),
        })

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    logger.info("ChromaDB seeded with %d documents from %s", len(ids), kb_docs_dir)


def _do_kb_search_local(query: str, max_results: int, settings) -> dict:
    """Search using local ChromaDB (no GCP required)."""
    try:
        collection = _get_chroma_collection(settings)
        n = min(max_results, collection.count())
        results = collection.query(query_texts=[query], n_results=n)

        docs = []
        for doc_id, meta, distance in zip(
            results["ids"][0], results["metadatas"][0], results["distances"][0]
        ):
            docs.append({
                "id": doc_id,
                "title": meta.get("title", ""),
                "snippet": meta.get("category", ""),
                "link": meta.get("link", ""),
                "score": round(1 - distance, 3),
            })

        logger.info("ChromaDB search returned %d results for query='%s'", len(docs), query)
        return {"query": query, "count": len(docs), "summary": "", "results": docs, "source": "local"}

    except Exception as exc:
        logger.error("ChromaDB search failed: %s", exc)
        return {"error": str(exc), "query": query, "count": 0, "results": []}


def _do_kb_search_gcp(query: str, max_results: int, settings) -> dict:
    """Search using Vertex AI Search (Discovery Engine) on GCP."""
    if not settings.gcp_project_id or not settings.kb_datastore_id:
        logger.error("KB_PROVIDER=gcp but GCP_PROJECT_ID is not set")
        return {"error": "GCP_PROJECT_ID is required when KB_PROVIDER=gcp", "query": query, "count": 0, "results": []}
    try:
        from google.cloud import discoveryengine_v1 as discoveryengine

        client = discoveryengine.SearchServiceClient()
        serving_config_name = client.serving_config_path(
            project=settings.gcp_project_id,
            location="global",
            data_store=settings.kb_datastore_id,
            serving_config=settings.kb_serving_config,
        )
        request = discoveryengine.SearchRequest(
            serving_config=serving_config_name,
            query=query,
            page_size=max_results,
            content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
                snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                    return_snippet=True, max_snippet_count=2
                ),
                summary_spec=discoveryengine.SearchRequest.ContentSearchSpec.SummarySpec(
                    include_citations=True,
                    summary_result_count=max_results,
                    ignore_adversarial_query=True,
                ),
            ),
        )
        response = client.search(request)

        results = []
        for r in response.results:
            derived = r.document.derived_struct_data

            def _field(name: str) -> str:
                val = derived.get(name)
                if val is None:
                    return ""
                if hasattr(val, "list_value") and val.list_value.values:
                    parts = []
                    for v in val.list_value.values:
                        if hasattr(v, "struct_value"):
                            s = v.struct_value.get("snippet")
                            parts.append(s.string_value if s else "")
                        else:
                            parts.append(v.string_value)
                    return " ".join(p for p in parts if p)
                return val.string_value if hasattr(val, "string_value") else str(val)

            results.append({
                "id": r.id,
                "title": _field("title"),
                "snippet": _field("snippets"),
                "link": _field("link"),
            })

        summary = ""
        if hasattr(response, "summary") and response.summary:
            summary = response.summary.summary_text

        logger.info("Vertex AI Search returned %d results for query='%s'", len(results), query)
        return {"query": query, "count": len(results), "summary": summary, "results": results, "source": "gcp"}

    except Exception as exc:
        logger.error("Vertex AI Search failed: %s", exc)
        return {"error": str(exc), "query": query, "count": 0, "results": []}


def _do_kb_search(query: str, max_results: int, settings) -> dict:
    """Dispatch to local ChromaDB or GCP Vertex AI Search based on KB_PROVIDER."""
    if settings.kb_provider == "gcp":
        return _do_kb_search_gcp(query, max_results, settings)
    return _do_kb_search_local(query, max_results, settings)


def create_kb_tool(settings) -> StructuredTool:
    """
    Native LangChain tool for Knowledge Base search.
    KB_PROVIDER=local  → ChromaDB (embedded, kb-docs/ seeded on first call)
    KB_PROVIDER=gcp    → Vertex AI Search (Discovery Engine)
    """
    class KbSearchInput(BaseModel):
        query: str = Field(description="Natural language search query")
        max_results: int = Field(default=3, description="Max docs to return (default 3)")

    def _sync(query: str, max_results: int = 3) -> str:
        return json.dumps(_do_kb_search(query, max_results, settings))

    async def _async(query: str, max_results: int = 3) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: json.dumps(_do_kb_search(query, max_results, settings))
        )

    _sync.__name__ = "searchKnowledgeBase"
    _async.__name__ = "searchKnowledgeBase"

    return StructuredTool(
        name="searchKnowledgeBase",
        description="Searches the AdTech support knowledge base for troubleshooting guides, policy docs, and how-to articles.",
        args_schema=KbSearchInput,
        func=_sync,
        coroutine=_async,
    )
