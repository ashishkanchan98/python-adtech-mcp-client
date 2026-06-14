=========================================================
  python-adtech-mcp-client  —  Build & Deploy Guide
  Python / FastAPI / LangChain / LangGraph
=========================================================

WHAT THIS SERVICE DOES
  Python reimplementation of adtech-mcp-client using
  LangChain + LangGraph instead of Java Spring Boot.

  Same AI behavior. Same REST API surface.
  Same adtech-mcp-server backend (unchanged).

  Endpoints:
    POST /api/v1/support/query   — run LangGraph agent
    GET  /api/v1/support/tickets — list tickets (SQLite)
    GET  /api/v1/support/health-check
    GET  /                       — web dashboard
    GET  /docs                   — FastAPI Swagger UI

  MUST deploy adtech-mcp-server (Java) before this service.
  This service calls adtech-mcp-server for all tool execution.

=========================================================
  HOW IT DIFFERS FROM THE JAVA CLIENT
=========================================================

  Java (adtech-mcp-client)           Python (python-adtech-mcp-client)
  ─────────────────────────────      ────────────────────────────────────
  Spring Boot + AgentOrchestrator    FastAPI + LangGraph StateGraph
  Manual while(toolCallRound<max)    Conditional edge + ToolNode
  @Async + CompletableFuture         ToolNode parallel async natively
  GroqClient (manual JSON build)     LangChain ChatGroq / ChatVertexAI
  H2 in-memory DB (JPA)             SQLite async (SQLAlchemy + aiosqlite)
  VertexAIClient (protobuf manual)   langchain-google-vertexai (native)
  No Swagger UI                      /docs (automatic via FastAPI)

=========================================================
  PREREQUISITES
=========================================================

1. adtech-mcp-server deployed on Cloud Run
   cd ../adtech-mcp-server && ./deploy.sh

2. GROQ_API_KEY (for Groq mode — default)
   Free key at https://console.groq.com/keys
   export GROQ_API_KEY=gsk_...

   OR use Vertex AI (no API key needed, uses ADC):
   export LLM_PROVIDER=vertex

3. gcloud CLI authenticated
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project YOUR_PROJECT_ID

=========================================================
  OPTION A — One-command deploy (recommended)
=========================================================

  cd python-adtech-mcp-client
  export GROQ_API_KEY=gsk_...         # required for Groq mode
  # export LLM_PROVIDER=vertex        # optional: use Gemini instead
  # export MCP_SERVER_URL=https://... # optional: auto-resolved
  chmod +x deploy.sh
  ./deploy.sh

  What it does:
  1. Validates GROQ_API_KEY (if LLM_PROVIDER=groq)
  2. Auto-resolves MCP server URL from Cloud Run
  3. Stores GROQ_API_KEY securely in Secret Manager
  4. Enables required GCP APIs
  5. Submits to Cloud Build: Docker build → GCR push → Cloud Run deploy
  6. Health check

=========================================================
  OPTION B — Manual step-by-step
=========================================================

  export PROJECT_ID=$(gcloud config get project)
  export GROQ_API_KEY=gsk_...

  # Get MCP server URL
  export MCP_SERVER_URL=$(gcloud run services describe adtech-mcp-server \
    --region us-central1 --format "value(status.url)")

  # Store GROQ key in Secret Manager
  echo -n "$GROQ_API_KEY" | gcloud secrets create groq-api-key \
    --data-file=- --replication-policy=automatic

  # Grant access
  PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
  gcloud secrets add-iam-policy-binding groq-api-key \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"

  # Enable APIs
  gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
    containerregistry.googleapis.com secretmanager.googleapis.com

  # Build + deploy
  gcloud builds submit \
    --config cloudbuild.yaml \
    --substitutions "_MCP_SERVER_URL=$MCP_SERVER_URL" \
    --timeout=15m \
    .

=========================================================
  LOCAL RUN
=========================================================

  # Start adtech-mcp-server first (Terminal 1):
  cd ../adtech-mcp-server
  mvn clean package -DskipTests
  java -jar target/adtech-mcp-server-1.0.0.jar

  # Start Python client (Terminal 2):
  cd python-adtech-mcp-client
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt

  cp .env.example .env
  # Edit .env — set GROQ_API_KEY=gsk_...

  uvicorn app.main:app --reload --port 8080
  # Dashboard: http://localhost:8080
  # Swagger:   http://localhost:8080/docs

=========================================================
  TEST THE DEPLOYED SERVICE
=========================================================

  CLIENT_URL=$(gcloud run services describe python-adtech-mcp-client \
    --region us-central1 --format "value(status.url)")

  # Health check (shows LLM provider + tool count)
  curl $CLIENT_URL/api/v1/support/health-check | python3 -m json.tool

  # Test — campaign not delivering
  curl -X POST "$CLIENT_URL/api/v1/support/query" \
    -H "Content-Type: application/json" \
    -d '{
      "query": "My campaign is not delivering any impressions",
      "campaignId": "CMP-4491",
      "advertiserId": "ADV-8821",
      "accountTier": "ENTERPRISE"
    }' | python3 -m json.tool

  # Test — PMP deal issue
  curl -X POST "$CLIENT_URL/api/v1/support/query" \
    -H "Content-Type: application/json" \
    -d '{"query":"No bids on my PMP deal","campaignId":"CMP-4493"}'

  # List all tickets
  curl $CLIENT_URL/api/v1/support/tickets | python3 -m json.tool

  # Swagger UI
  open $CLIENT_URL/docs

=========================================================
  SWITCH TO VERTEX AI (Gemini)
=========================================================

  # 1. Set LLM_PROVIDER=vertex before deploying:
  export LLM_PROVIDER=vertex
  ./deploy.sh

  # What changes:
  # - No GROQ_API_KEY needed
  # - Uses Application Default Credentials (ADC) — automatic on Cloud Run
  # - Model: gemini-2.0-flash-001 (set via VERTEX_MODEL env var)
  # - Same LangGraph graph — only the LLM object changes

  # To also set project/model:
  gcloud run services update python-adtech-mcp-client \
    --region us-central1 \
    --update-env-vars "LLM_PROVIDER=vertex,VERTEX_PROJECT_ID=$PROJECT_ID,VERTEX_MODEL=gemini-2.0-flash-001"

=========================================================
  ENVIRONMENT VARIABLES
=========================================================

  Variable           | Required | Default                    | Description
  -------------------|----------|----------------------------|----------------------------
  GROQ_API_KEY       | Groq     | —                          | From Secret Manager
  LLM_PROVIDER       | no       | groq                       | 'groq' or 'vertex'
  GROQ_MODEL         | no       | llama-3.3-70b-versatile    | Groq model name
  VERTEX_PROJECT_ID  | Vertex   | —                          | GCP project ID
  VERTEX_LOCATION    | no       | us-central1                | Vertex AI region
  VERTEX_MODEL       | no       | gemini-2.0-flash-001       | Gemini model (pin version!)
  MCP_SERVER_URL     | YES      | http://localhost:8081      | adtech-mcp-server URL
  AGENT_MAX_TOOL_CALLS| no      | 6                          | Max LangGraph loop rounds
  PORT               | no       | 8080                       | Injected by Cloud Run

=========================================================
  LANGGRAPH AGENT FLOW
=========================================================

  START
    |
    v
  [agent node]   call_model()
    |   Prepends SYSTEM_PROMPT to state["messages"]
    |   Calls LLM.invoke() → AIMessage
    |
    +-- AIMessage has tool_calls? ──── YES ──► [tools node]
    |                                             ToolNode executes all tool_calls
    |                                             in parallel (async coroutines)
    |                                             Each: POST /mcp/tools/{name}
    |                                             Returns ToolMessages to state
    |           ◄──────────────────────────────────┘  (loop back)
    |
    +-- no tool_calls OR max rounds ──► END
           |
           v
         extract_results(state["messages"])
           → final_answer (last AIMessage.content)
           → tools_used   (all tool_call names, deduplicated)

  Key source files:
    app/agent/graph.py    build_agent_graph()  — LangGraph definition
    app/agent/tools.py    create_mcp_tools()   — StructuredTool factory
    app/agent/prompts.py  SYSTEM_PROMPT        — system instruction
    app/api/routes.py     POST /query          — FastAPI entry point
    app/db/models.py      Ticket               — SQLAlchemy model

=========================================================
  COST MANAGEMENT
=========================================================

  # Scale to zero (no cost when idle)
  gcloud run services update python-adtech-mcp-client \
    --region us-central1 --min-instances 0

  # Delete service
  gcloud run services delete python-adtech-mcp-client --region us-central1

  # Delete GROQ secret (if no longer needed)
  gcloud secrets delete groq-api-key



  Local dev — zero GCP needed:
  cp .env.example .env
  # KB_PROVIDER=local is the default — nothing else to set

  uvicorn app.main:app --reload --port 8080
  # First searchKnowledgeBase call → downloads all-MiniLM-L6-v2 (~90MB once)
  # → seeds chroma_db/ from kb-docs/
  # → real semantic search from that point on

  GCP deploy — automatic:
  ./deploy.sh   # injects KB_PROVIDER=gcp + GCP_PROJECT_ID + KB_DATASTORE_ID
                # Cloud Run uses Vertex AI Search, no chroma_db needed

  Switching on local to test GCP path:
  # in .env
  KB_PROVIDER=gcp
  GCP_PROJECT_ID=your-project-id
  # run: gcloud auth application-default login first








   cd /Users/ashish.kanchan/eclipse-workspace/python-adtech-mcp-client

  # Create venv
  python3 -m venv venv
  source venv/bin/activate

  # Install all dependencies
  pip install -r requirements.txt

  # Then start
  uvicorn app.main:app --reload --port 8080

  And for the server in a separate terminal:

  cd /Users/ashish.kanchan/eclipse-workspace/python-adtech-mcp-server

  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt

  uvicorn app.main:app --reload --port 8082