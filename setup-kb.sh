#!/bin/bash
# =============================================================================
# setup-kb.sh — Create and seed the Vertex AI Search datastore
#               for python-adtech-mcp-client
#
# Run this ONCE before deploying the client (or when adding new KB docs).
# The searchKnowledgeBase tool in app/agent/tools.py queries this datastore.
#
# What this script does:
#   1. Creates the Vertex AI Search datastore  (adtech-kb)
#   2. Creates a GCS bucket to hold KB documents
#   3. Uploads all JSON documents from kb-docs/ to GCS
#   4. Imports and indexes the documents into Vertex AI Search
#   5. Polls until indexing is complete
#   6. Runs test searches to verify it's working
#
# Usage:
#   export PROJECT_ID=your-gcp-project-id
#   ./setup-kb.sh
#
# kb-docs/ location: the script looks in:
#   1. ./kb-docs/              (local copy — if you put docs here)
#   2. ../adtech-support-poc/kb-docs/   (shared source — default)
# =============================================================================
set -e

# ── Configuration ──────────────────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:?ERROR: Set PROJECT_ID. e.g. export PROJECT_ID=your-gcp-project}"
LOCATION="global"
COLLECTION="default_collection"
DATASTORE_ID="${KB_DATASTORE_ID:-adtech-kb}"
BUCKET="gs://${PROJECT_ID}-kb-docs"
BASE_URL="https://discoveryengine.googleapis.com/v1"
DATASTORE_PATH="projects/${PROJECT_ID}/locations/${LOCATION}/collections/${COLLECTION}/dataStores/${DATASTORE_ID}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve kb-docs — prefer local copy, fall back to shared adtech-support-poc docs
if [ -d "${SCRIPT_DIR}/kb-docs" ]; then
  KB_DOCS_DIR="${SCRIPT_DIR}/kb-docs"
elif [ -d "${SCRIPT_DIR}/../adtech-support-poc/kb-docs" ]; then
  KB_DOCS_DIR="$(cd "${SCRIPT_DIR}/../adtech-support-poc/kb-docs" && pwd)"
else
  echo "ERROR: kb-docs/ not found."
  echo "  Expected at: ./kb-docs/"
  echo "  Or at:       ../adtech-support-poc/kb-docs/"
  exit 1
fi

echo ""
echo "============================================================"
echo " python-adtech-mcp-client — Knowledge Base Setup"
echo "============================================================"
echo " Project  : $PROJECT_ID"
echo " Datastore: $DATASTORE_ID"
echo " Bucket   : $BUCKET"
echo " Docs dir : $KB_DOCS_DIR"
echo "============================================================"
echo ""

# ── Enable required API ────────────────────────────────────────────────────────
echo "==> Enabling Discovery Engine API..."
gcloud services enable discoveryengine.googleapis.com --quiet
echo "    API enabled"

# ── Get auth token ────────────────────────────────────────────────────────────
echo "==> Authenticating..."
TOKEN=$(gcloud auth print-access-token)
if [ -z "$TOKEN" ]; then
  echo "ERROR: Could not get access token."
  echo "  Run: gcloud auth application-default login"
  exit 1
fi
echo "    Auth OK"

# ── Step 1: Create datastore ──────────────────────────────────────────────────
echo ""
echo "==> Step 1: Creating Vertex AI Search datastore '$DATASTORE_ID'..."

HTTP_STATUS=$(curl -s -o /tmp/ds_response.json -w "%{http_code}" \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "${BASE_URL}/projects/${PROJECT_ID}/locations/${LOCATION}/collections/${COLLECTION}/dataStores?dataStoreId=${DATASTORE_ID}" \
  -d '{
    "displayName": "AdTech Support Knowledge Base",
    "industryVertical": "GENERIC",
    "contentConfig": "CONTENT_REQUIRED",
    "solutionTypes": ["SOLUTION_TYPE_SEARCH"]
  }')

if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "409" ]; then
  echo "    Datastore ready (HTTP $HTTP_STATUS)"
else
  echo "ERROR: Datastore creation failed (HTTP $HTTP_STATUS)"
  cat /tmp/ds_response.json
  exit 1
fi

# ── Step 2: Wait for provisioning ────────────────────────────────────────────
echo ""
echo "==> Step 2: Waiting 30s for datastore to provision..."
sleep 30
echo "    Done waiting"

# ── Step 3: Create GCS bucket + upload docs ───────────────────────────────────
echo ""
echo "==> Step 3: Creating GCS bucket and uploading KB documents..."

gsutil mb -p "$PROJECT_ID" "$BUCKET" 2>/dev/null || echo "    Bucket already exists — skipping creation"

DOC_COUNT=$(ls "${KB_DOCS_DIR}"/*.json 2>/dev/null | wc -l | tr -d ' ')
echo "    Found $DOC_COUNT documents in $KB_DOCS_DIR"

gsutil -m cp "${KB_DOCS_DIR}"/*.json "${BUCKET}/"
echo "    Uploaded $DOC_COUNT documents to $BUCKET"

echo ""
echo "    Documents uploaded:"
gsutil ls "$BUCKET" | while read f; do
  echo "      $f"
done

# ── Step 4: Import documents into Vertex AI Search ────────────────────────────
echo ""
echo "==> Step 4: Importing documents into Vertex AI Search..."
echo "    This triggers chunking, embedding, and index construction."

TOKEN=$(gcloud auth print-access-token)  # refresh

IMPORT_RESPONSE=$(curl -s \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "${BASE_URL}/${DATASTORE_PATH}/branches/default_branch/documents:import" \
  -d "{
    \"gcsSource\": {
      \"inputUris\": [\"${BUCKET}/*.json\"],
      \"dataSchema\": \"document\"
    },
    \"reconciliationMode\": \"FULL\"
  }")

OPERATION_NAME=$(echo "$IMPORT_RESPONSE" | python3 -c \
  "import sys, json; print(json.load(sys.stdin).get('name', ''))" 2>/dev/null)

if [ -z "$OPERATION_NAME" ]; then
  echo "ERROR: Import failed. Response:"
  echo "$IMPORT_RESPONSE"
  exit 1
fi
echo "    Import operation started: $OPERATION_NAME"

# ── Step 5: Poll until indexing completes ─────────────────────────────────────
echo ""
echo "==> Step 5: Waiting for indexing to complete (checks every 30s, max 10 min)..."

MAX_WAIT=600
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
  TOKEN=$(gcloud auth print-access-token)

  OPERATION_STATUS=$(curl -s \
    -H "Authorization: Bearer $TOKEN" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "${BASE_URL}/${OPERATION_NAME}")

  DONE=$(echo "$OPERATION_STATUS" | python3 -c \
    "import sys, json; print(json.load(sys.stdin).get('done', False))" 2>/dev/null)

  if [ "$DONE" = "True" ]; then
    echo "    Indexing complete! ($ELAPSED seconds elapsed)"
    ERROR=$(echo "$OPERATION_STATUS" | python3 -c \
      "import sys, json; d=json.load(sys.stdin); print(d.get('error', {}).get('message', ''))" 2>/dev/null)
    if [ -n "$ERROR" ]; then
      echo "    WARNING: Operation completed with error: $ERROR"
    fi
    break
  fi

  echo "    Still indexing... (${ELAPSED}s / ${MAX_WAIT}s)"
  sleep 30
  ELAPSED=$((ELAPSED + 30))
done

if [ $ELAPSED -ge $MAX_WAIT ]; then
  echo "WARNING: Timed out after ${MAX_WAIT}s. Indexing may still be in progress."
  echo "  Check: gcloud alpha discovery-engine datastores list --project=$PROJECT_ID"
fi

# ── Step 6: Verify with test searches ────────────────────────────────────────
echo ""
echo "==> Step 6: Running verification searches..."

TOKEN=$(gcloud auth print-access-token)

run_test_search() {
  local QUERY="$1"

  RESPONSE=$(curl -s \
    -X POST \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "${BASE_URL}/${DATASTORE_PATH}/servingConfigs/default_search:search" \
    -d "{\"query\": \"${QUERY}\", \"pageSize\": 1}")

  RESULT_COUNT=$(echo "$RESPONSE" | python3 -c \
    "import sys, json; print(len(json.load(sys.stdin).get('results', [])))" 2>/dev/null)

  TOP_TITLE=$(echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
results = data.get('results', [])
if results:
    doc = results[0].get('document', {})
    struct = doc.get('derivedStructData', {})
    title_field = struct.get('title', {})
    if isinstance(title_field, dict):
        print(title_field.get('stringValue', 'N/A'))
    else:
        print(title_field or 'N/A')
else:
    print('NO RESULTS')
" 2>/dev/null)

  if [ "$RESULT_COUNT" -gt "0" ] 2>/dev/null; then
    echo "    OK  '$QUERY'"
    echo "        Top result: $TOP_TITLE"
  else
    echo "    --  '$QUERY' — no results (may still be indexing)"
  fi
}

run_test_search "campaign not delivering impressions"        # doc-001
run_test_search "bid below floor price"                    # doc-002
run_test_search "budget exhausted early morning"           # doc-003
run_test_search "audience segment SHA-256 hash error"      # doc-004
run_test_search "PMP deal zero bid requests seat mismatch" # doc-005
run_test_search "pixel fires but conversions not credited" # doc-006
run_test_search "creative disapproved rejection reason"    # doc-007
run_test_search "frequency cap additive line item"         # doc-008
run_test_search "invalid traffic IVT SIVT bot fraud"       # doc-009
run_test_search "DSP GAM impression discrepancy reporting" # doc-010

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " Knowledge Base Setup Complete"
echo "============================================================"
echo ""
echo "  Datastore ID : $DATASTORE_ID"
echo "  GCS Bucket   : $BUCKET"
echo "  Documents    : $DOC_COUNT indexed"
echo ""
echo "  Set these in your .env (local) or Cloud Run deploy:"
echo "    GCP_PROJECT_ID=$PROJECT_ID"
echo "    KB_DATASTORE_ID=$DATASTORE_ID"
echo "    KB_SERVING_CONFIG=default_search"
echo ""
echo "  Local test (after setting .env):"
echo "    uvicorn app.main:app --reload --port 8080"
echo ""
echo "  Cloud Run — already injected by deploy.sh via:"
echo "    --set-env-vars=GCP_PROJECT_ID=\$PROJECT_ID,KB_DATASTORE_ID=$DATASTORE_ID"
echo ""
echo "  To add more documents later:"
echo "    1. Add JSON files to kb-docs/"
echo "    2. gsutil cp kb-docs/new-doc.json $BUCKET/"
echo "    3. Re-run this script (FULL mode — replaces all)"
echo ""
echo "  To query the datastore directly:"
echo "    TOKEN=\$(gcloud auth print-access-token)"
echo "    curl -X POST -H \"Authorization: Bearer \$TOKEN\" \\"
echo "      -H \"Content-Type: application/json\" \\"
echo "      \"${BASE_URL}/${DATASTORE_PATH}/servingConfigs/default_search:search\" \\"
echo "      -d '{\"query\": \"your question here\", \"pageSize\": 3}'"
echo "============================================================"
