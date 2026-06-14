#!/bin/bash
# ============================================================
#  python-adtech-mcp-client — GCP Build & Deploy Script
#  Run AFTER python-adtech-mcp-server is deployed.
#
#  Dev deploy (default):
#    export GROQ_API_KEY=gsk_...
#    ./deploy.sh
#
#  Production deploy:
#    export PROD=true
#    export LLM_PROVIDER=vertex
#    export CORS_ORIGINS=https://adtech-support.yourdomain.com
#    export CLOUDSQL_INSTANCE=PROJECT:us-central1:adtech-db
#    export DATABASE_URL=postgresql+asyncpg://user:pass@/adtech?host=/cloudsql/PROJECT:us-central1:adtech-db
#    ./deploy.sh
#
#  Optional for both:
#    export MCP_SERVER_URL=https://...        # auto-resolved if not set
#    export REGION=us-central1
#    export KB_DATASTORE_ID=adtech-kb
#    export SERVER_SERVICE=adtech-mcp-server  # to use Java server
# ============================================================
set -e

REGION="${REGION:-us-central1}"
LLM_PROVIDER="${LLM_PROVIDER:-groq}"
KB_DATASTORE_ID="${KB_DATASTORE_ID:-adtech-kb}"
SERVICE="python-adtech-mcp-client"
SERVER_SERVICE="${SERVER_SERVICE:-python-adtech-mcp-server}"

# ── Production vs dev settings ───────────────────────────────
if [ "${PROD:-false}" = "true" ]; then
  ENV="prod"
  MIN_INSTANCES="1"
  MAX_INSTANCES="10"
  MEMORY="1Gi"
  CPU="2"
  CORS_ORIGINS="${CORS_ORIGINS:-*}"
  DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///./tickets.db}"
  CLOUDSQL_INSTANCE="${CLOUDSQL_INSTANCE:-}"
  API_KEY_SECRET="${API_KEY_SECRET:-}"
  echo ">>> PRODUCTION mode"
else
  ENV="dev"
  MIN_INSTANCES="0"
  MAX_INSTANCES="3"
  MEMORY="512Mi"
  CPU="1"
  CORS_ORIGINS="*"
  DATABASE_URL="sqlite+aiosqlite:///./tickets.db"
  CLOUDSQL_INSTANCE=""
  API_KEY_SECRET=""
fi

# ── Validate GROQ key (Groq mode only) ──────────────────────
if [ "$LLM_PROVIDER" = "groq" ] && [ -z "$GROQ_API_KEY" ]; then
  echo "ERROR: GROQ_API_KEY is not set."
  echo "  Get a free key at https://console.groq.com/keys"
  echo "  Then run: export GROQ_API_KEY=gsk_..."
  echo ""
  echo "  For production with Vertex AI: export LLM_PROVIDER=vertex"
  exit 1
fi

# ── Validate GCP project ─────────────────────────────────────
export PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: No active GCP project."
  echo "  Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

# ── Resolve MCP server URL ───────────────────────────────────
if [ -z "$MCP_SERVER_URL" ]; then
  echo "Auto-resolving MCP server URL..."
  MCP_SERVER_URL=$(gcloud run services describe "$SERVER_SERVICE" \
    --platform managed \
    --region "$REGION" \
    --format "value(status.url)" 2>/dev/null || echo "")

  if [ -z "$MCP_SERVER_URL" ]; then
    echo "ERROR: '$SERVER_SERVICE' not found in region $REGION."
    echo "  Deploy it first:  cd ../python-adtech-mcp-server && ./deploy.sh"
    exit 1
  fi
fi

echo ""
echo "============================================"
echo "  python-adtech-mcp-client  →  GCP Deploy"
echo "  Project        : $PROJECT_ID"
echo "  Region         : $REGION"
echo "  Environment    : $ENV"
echo "  Service        : $SERVICE"
echo "  LLM Provider   : $LLM_PROVIDER"
echo "  MCP Server URL : $MCP_SERVER_URL"
echo "  KB Provider    : gcp"
echo "  KB Datastore   : $KB_DATASTORE_ID"
echo "  Min Instances  : $MIN_INSTANCES"
echo "  Memory         : $MEMORY  CPU: $CPU"
if [ -n "$CLOUDSQL_INSTANCE" ]; then
echo "  Cloud SQL      : $CLOUDSQL_INSTANCE"
fi
echo "============================================"
echo ""

# ── Step 1: Store GROQ key in Secret Manager (Groq mode) ────
if [ "$LLM_PROVIDER" = "groq" ]; then
  echo "[1/5] Storing GROQ_API_KEY in Secret Manager..."
  if gcloud secrets describe groq-api-key --project="$PROJECT_ID" &>/dev/null; then
    echo "  Secret 'groq-api-key' exists — updating value..."
    echo -n "$GROQ_API_KEY" | gcloud secrets versions add groq-api-key --data-file=-
  else
    echo "  Creating secret 'groq-api-key'..."
    echo -n "$GROQ_API_KEY" | gcloud secrets create groq-api-key \
      --data-file=- \
      --replication-policy=automatic
  fi

  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
  CR_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
  CB_SA="${PROJECT_ID}@cloudbuild.gserviceaccount.com"

  gcloud secrets add-iam-policy-binding groq-api-key \
    --member="serviceAccount:$CR_SA" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet 2>/dev/null || true

  gcloud secrets add-iam-policy-binding groq-api-key \
    --member="serviceAccount:$CB_SA" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet 2>/dev/null || true
else
  echo "[1/5] Vertex AI mode — skipping GROQ Secret Manager (using ADC)"
fi

# ── Step 2: Enable required APIs ────────────────────────────
echo "[2/5] Enabling APIs..."
APIS="run.googleapis.com cloudbuild.googleapis.com containerregistry.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com discoveryengine.googleapis.com"
if [ -n "$CLOUDSQL_INSTANCE" ]; then
  APIS="$APIS sqladmin.googleapis.com"
fi
gcloud services enable $APIS --quiet

# ── Step 3: Build + push + deploy via Cloud Build ───────────
echo "[3/5] Building and deploying via Cloud Build..."
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions \
    "_REGION=$REGION,\
_MCP_SERVER_URL=$MCP_SERVER_URL,\
_LLM_PROVIDER=$LLM_PROVIDER,\
_KB_DATASTORE_ID=$KB_DATASTORE_ID,\
_ENV=$ENV,\
_MIN_INSTANCES=$MIN_INSTANCES,\
_MAX_INSTANCES=$MAX_INSTANCES,\
_MEMORY=$MEMORY,\
_CPU=$CPU,\
_CORS_ORIGINS=$CORS_ORIGINS,\
_DATABASE_URL=$DATABASE_URL,\
_CLOUDSQL_INSTANCE=$CLOUDSQL_INSTANCE,\
_API_KEY_SECRET=$API_KEY_SECRET" \
  --timeout=15m \
  .

# ── Step 4: Verify ──────────────────────────────────────────
echo "[4/5] Verifying deployment..."
CLIENT_URL=$(gcloud run services describe "$SERVICE" \
  --region "$REGION" \
  --format "value(status.url)" 2>/dev/null || echo "")

if [ -z "$CLIENT_URL" ]; then
  echo "ERROR: Service URL not found. Check Cloud Build logs."
  exit 1
fi

# ── Step 5: Health check ────────────────────────────────────
echo "[5/5] Health check..."
sleep 4
HEALTH=$(curl -sf "$CLIENT_URL/api/v1/support/health-check" 2>/dev/null || echo "unreachable")
echo "  Health: $HEALTH"

echo ""
echo "============================================"
echo "  Python MCP Client is LIVE [$ENV]"
echo "  URL      : $CLIENT_URL"
echo "  Dashboard: $CLIENT_URL/"
if [ "$ENV" != "prod" ]; then
echo "  Swagger  : $CLIENT_URL/docs"
fi
echo ""
echo "  Quick test:"
echo "  curl -X POST $CLIENT_URL/api/v1/support/query \\"
echo "    -H 'Content-Type: application/json' \\"
if [ -n "$API_KEY_SECRET" ]; then
echo "    -H 'X-API-Key: YOUR_API_KEY' \\"
fi
echo "    -d '{\"query\":\"Campaign CMP-4491 not delivering\",\"campaignId\":\"CMP-4491\"}'"
echo "============================================"
echo ""
