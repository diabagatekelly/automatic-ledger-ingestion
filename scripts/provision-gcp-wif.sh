#!/usr/bin/env bash
# Provision keyless CD (Workload Identity Federation) for automatic-ledger-ingestion.
#
# Run ONCE, locally, after authenticating:
#     gcloud auth login
#     gcloud config set project <YOUR_PROJECT_ID>
#     bash scripts/provision-gcp-wif.sh
#
# It is IDEMPOTENT (safe to re-run) and does nothing destructive. It:
#   * enables the APIs a Gen2 Cloud Functions deploy needs
#   * creates a least-privilege deployer service account for GitHub Actions
#   * creates a Workload Identity pool + provider that trusts ONLY this repo
#   * binds the deployer SA to that repo (no key ever leaves GCP)
#   * stores the WhatsApp verify token in Secret Manager + grants the runtime SA
#   * prints the five repo secrets to set — and sets them via `gh` if you pass
#     --set-gh-secrets (requires `gh auth login`).
#
# Prereq NOT done here (one-time, manual): share the Sheet with the runtime SA
# ($RUNTIME_SA_EMAIL) as Editor. For automatic-ledger-ingestion that was already done for
# Issue #1.
set -euo pipefail

# ---- config (override any of these via environment) -------------------------
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
GITHUB_REPO="${GITHUB_REPO:-diabagatekelly/automatic-ledger-ingestion}"
REGION="${REGION:-us-central1}"
POOL_ID="${POOL_ID:-github-pool}"
PROVIDER_ID="${PROVIDER_ID:-github-provider}"
DEPLOYER_SA_NAME="${DEPLOYER_SA_NAME:-gh-deployer}"
# Runtime identity of the function — MUST already be Editor on the Sheet.
RUNTIME_SA_EMAIL="${RUNTIME_SA_EMAIL:-ledger-writer@${PROJECT_ID}.iam.gserviceaccount.com}"
SET_GH_SECRETS=0
[ "${1:-}" = "--set-gh-secrets" ] && SET_GH_SECRETS=1

# Read a value from .env robustly: last matching line wins; strip the trailing
# CR that Windows editors leave (CRLF) and one layer of surrounding quotes.
# (Plain grep|cut would leak a \r into the secret value on Windows.)
read_env_var() {
  [ -f .env ] || return 0
  local line
  line="$(grep -E "^$1=" .env | tail -n1)" || return 0
  line="${line#*=}"
  line="${line%$'\r'}"
  case "$line" in
    \"*\") line="${line#\"}"; line="${line%\"}" ;;
    \'*\') line="${line#\'}"; line="${line%\'}" ;;
  esac
  printf '%s' "$line"
}

# Retry a command through GCP eventual consistency — e.g. a just-created service
# account not yet visible to the IAM policy backend ("... does not exist" right
# after create). Progress goes to stderr so callers can still redirect stdout.
retry() {
  local n=0 max=6 delay=5
  until "$@"; do
    n=$((n + 1))
    if [ "$n" -ge "$max" ]; then
      echo "ERROR: still failing after $max attempts: $*" >&2
      return 1
    fi
    echo "   transient error — retry $n/$max in ${delay}s..." >&2
    sleep "$delay"
  done
}

# Store NAME=VALUE in Secret Manager idempotently: create the secret if absent,
# add a new version only when the value actually changed (so re-runs don't
# clutter version history), and grant the runtime SA read access. `access
# latest` fails on a brand-new secret (no versions yet) -> current stays empty
# -> treated as changed.
store_secret() {
  local name="$1" value="$2" current
  echo "-- Secret Manager: $name"
  gcloud secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1 \
    || gcloud secrets create "$name" --project="$PROJECT_ID" --replication-policy=automatic
  current="$(gcloud secrets versions access latest \
    --secret="$name" --project="$PROJECT_ID" 2>/dev/null || true)"
  if [ "$value" = "$current" ]; then
    echo "   value unchanged — skipping new version"
  else
    printf '%s' "$value" \
      | gcloud secrets versions add "$name" --project="$PROJECT_ID" --data-file=-
  fi
  echo "   granting runtime SA read access"
  retry gcloud secrets add-iam-policy-binding "$name" --project="$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: no project set. Run: gcloud config set project <id>" >&2
  exit 1
fi

# Fail fast if --set-gh-secrets is requested but gh isn't usable.
if [ "$SET_GH_SECRETS" = "1" ]; then
  command -v gh >/dev/null 2>&1 \
    || { echo "ERROR: --set-gh-secrets needs the gh CLI, which was not found." >&2; exit 1; }
  gh auth status >/dev/null 2>&1 \
    || { echo "ERROR: gh is not authenticated. Run: gh auth login" >&2; exit 1; }
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
DEPLOYER_SA_EMAIL="${DEPLOYER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
# Gen2 builds run as the default compute SA unless overridden.
BUILD_SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "== Project: $PROJECT_ID ($PROJECT_NUMBER) | repo: $GITHUB_REPO =="

# Fail fast: the runtime SA must already exist (created in Issue #1) and be
# Editor on the Sheet. Catch a typo'd name / wrong project before mutating IAM.
if ! gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "ERROR: runtime SA '$RUNTIME_SA_EMAIL' not found in project '$PROJECT_ID'." >&2
  echo "       Re-run with RUNTIME_SA_EMAIL=<your Sheet-editor SA email> set." >&2
  exit 1
fi

# ---- 1. enable APIs ---------------------------------------------------------
# cloudresourcemanager + serviceusage are needed by `gcloud functions deploy`
# itself (the deployer SA resolves the project through them); the rest are the
# Gen2 build/runtime surface.
echo "-- enabling APIs"
gcloud services enable \
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com iam.googleapis.com sts.googleapis.com \
  --project "$PROJECT_ID"

# ---- 2. deployer service account -------------------------------------------
echo "-- deployer SA: $DEPLOYER_SA_EMAIL"
gcloud iam service-accounts describe "$DEPLOYER_SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$DEPLOYER_SA_NAME" --project "$PROJECT_ID" \
       --display-name="GitHub Actions deployer (automatic-ledger-ingestion)"

# A freshly created SA is eventually consistent; the IAM bindings below are
# wrapped in retry() to ride out the propagation lag.
echo "-- granting deployer roles (least privilege for a Gen2 deploy)"
# storage.objectAdmin (not storage.admin): the deployer only needs to read/write
# source objects in the Gen2 upload bucket, not manage bucket IAM/config. If a
# brand-new project's very first deploy fails creating the source bucket,
# temporarily grant roles/storage.admin for that one run, then revoke.
for role in \
  roles/run.admin \
  roles/cloudfunctions.developer \
  roles/artifactregistry.writer \
  roles/cloudbuild.builds.editor \
  roles/storage.objectAdmin ; do
  retry gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER_SA_EMAIL}" --role="$role" \
    --condition=None >/dev/null
done

# Deployer must be able to act AS the runtime SA (to set it as the function
# identity) and AS the build SA (to run the Cloud Build). Scoped to those SAs.
echo "-- scoping serviceAccountUser to runtime + build SAs"
for target in "$RUNTIME_SA_EMAIL" "$BUILD_SA_EMAIL"; do
  retry gcloud iam service-accounts add-iam-policy-binding "$target" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER_SA_EMAIL}" \
    --role="roles/iam.serviceAccountUser" >/dev/null
done

# The build SA needs to build the image, push it, and write build logs.
echo "-- granting build SA the roles Cloud Build needs"
for role in \
  roles/cloudbuild.builds.builder \
  roles/artifactregistry.writer \
  roles/logging.logWriter ; do
  retry gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${BUILD_SA_EMAIL}" --role="$role" \
    --condition=None >/dev/null
done

# ---- 3. Workload Identity Federation ---------------------------------------
echo "-- WIF pool: $POOL_ID"
gcloud iam workload-identity-pools describe "$POOL_ID" \
  --project="$PROJECT_ID" --location=global >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools create "$POOL_ID" \
       --project="$PROJECT_ID" --location=global \
       --display-name="GitHub Actions pool"

echo "-- WIF provider: $PROVIDER_ID (trusts only $GITHUB_REPO)"
gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
       --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" \
       --display-name="GitHub OIDC" \
       --issuer-uri="https://token.actions.githubusercontent.com" \
       --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
       --attribute-condition="assertion.repository=='${GITHUB_REPO}'"

echo "-- binding deployer SA to the repo's federated identity"
retry gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA_EMAIL" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}" >/dev/null

# ---- 4. runtime secret in Secret Manager -----------------------------------
if [ -z "${WHATSAPP_VERIFY_TOKEN:-}" ]; then
  WHATSAPP_VERIFY_TOKEN="$(read_env_var WHATSAPP_VERIFY_TOKEN)"
fi
if [ -z "${WHATSAPP_VERIFY_TOKEN:-}" ]; then
  read -rsp "WhatsApp verify token (stored in Secret Manager): " WHATSAPP_VERIFY_TOKEN; echo
fi
if [ -z "${WHATSAPP_VERIFY_TOKEN:-}" ]; then
  echo "ERROR: WHATSAPP_VERIFY_TOKEN is empty — refusing to store an unusable secret." >&2
  exit 1
fi
store_secret whatsapp-verify-token "$WHATSAPP_VERIFY_TOKEN"

# Gemini API key (Google AI Studio) — mounted as GEMINI_API_KEY on the function
# so it can parse notes (#4). The function tolerates a missing key by falling
# back to a raw-text row, but the deploy mounts gemini-api-key:latest, so the
# secret must exist.
if [ -z "${GEMINI_API_KEY:-}" ]; then
  GEMINI_API_KEY="$(read_env_var GEMINI_API_KEY)"
fi
if [ -z "${GEMINI_API_KEY:-}" ]; then
  read -rsp "Gemini API key (stored in Secret Manager): " GEMINI_API_KEY; echo
fi
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY is empty — refusing to store an unusable secret." >&2
  exit 1
fi
store_secret gemini-api-key "$GEMINI_API_KEY"

# WhatsApp access token (Meta) — mounted as WHATSAPP_ACCESS_TOKEN so the function
# can download inbound media (receipt photos, #5) from the Graph media endpoint.
# The deploy mounts whatsapp-access-token:latest, so the secret must exist.
if [ -z "${WHATSAPP_ACCESS_TOKEN:-}" ]; then
  WHATSAPP_ACCESS_TOKEN="$(read_env_var WHATSAPP_ACCESS_TOKEN)"
fi
if [ -z "${WHATSAPP_ACCESS_TOKEN:-}" ]; then
  read -rsp "WhatsApp access token (stored in Secret Manager): " WHATSAPP_ACCESS_TOKEN; echo
fi
if [ -z "${WHATSAPP_ACCESS_TOKEN:-}" ]; then
  echo "ERROR: WHATSAPP_ACCESS_TOKEN is empty — refusing to store an unusable secret." >&2
  exit 1
fi
store_secret whatsapp-access-token "$WHATSAPP_ACCESS_TOKEN"

# WhatsApp app secret (Meta App dashboard -> Settings -> Basic -> App secret) —
# mounted as WHATSAPP_APP_SECRET so the function can verify each POST's
# X-Hub-Signature-256 HMAC (#8). The deploy mounts whatsapp-app-secret:latest and
# the handler fails closed without it, so the secret MUST exist before deploy.
if [ -z "${WHATSAPP_APP_SECRET:-}" ]; then
  WHATSAPP_APP_SECRET="$(read_env_var WHATSAPP_APP_SECRET)"
fi
if [ -z "${WHATSAPP_APP_SECRET:-}" ]; then
  read -rsp "WhatsApp app secret (stored in Secret Manager): " WHATSAPP_APP_SECRET; echo
fi
if [ -z "${WHATSAPP_APP_SECRET:-}" ]; then
  echo "ERROR: WHATSAPP_APP_SECRET is empty — refusing to store an unusable secret." >&2
  exit 1
fi
store_secret whatsapp-app-secret "$WHATSAPP_APP_SECRET"

# ---- 5. output the repo secrets --------------------------------------------
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
SHEET_ID_VALUE="${SHEET_ID:-}"
[ -z "$SHEET_ID_VALUE" ] && SHEET_ID_VALUE="$(read_env_var SHEET_ID)"

cat <<EOF

============================================================================
Done. Set these as GitHub repo secrets (Settings -> Secrets -> Actions):

  GCP_WIF_PROVIDER = $WIF_PROVIDER
  GCP_DEPLOY_SA    = $DEPLOYER_SA_EMAIL
  GCP_PROJECT      = $PROJECT_ID
  GCP_RUNTIME_SA   = $RUNTIME_SA_EMAIL
  SHEET_ID         = ${SHEET_ID_VALUE:-<your sheet id>}
============================================================================
EOF

if [ "$SET_GH_SECRETS" = "1" ]; then
  echo "-- setting repo secrets via gh"
  gh secret set GCP_WIF_PROVIDER --repo "$GITHUB_REPO" --body "$WIF_PROVIDER"
  gh secret set GCP_DEPLOY_SA    --repo "$GITHUB_REPO" --body "$DEPLOYER_SA_EMAIL"
  gh secret set GCP_PROJECT      --repo "$GITHUB_REPO" --body "$PROJECT_ID"
  gh secret set GCP_RUNTIME_SA   --repo "$GITHUB_REPO" --body "$RUNTIME_SA_EMAIL"
  [ -n "$SHEET_ID_VALUE" ] && gh secret set SHEET_ID --repo "$GITHUB_REPO" --body "$SHEET_ID_VALUE"
  echo "-- repo secrets set."
fi
