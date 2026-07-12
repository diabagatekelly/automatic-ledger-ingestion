#!/usr/bin/env bash
# Provision keyless CD (Workload Identity Federation) for catering-ledger.
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
# ($RUNTIME_SA_EMAIL) as Editor. For catering-ledger that was already done for
# Issue #1.
set -euo pipefail

# ---- config (override any of these via environment) -------------------------
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
GITHUB_REPO="${GITHUB_REPO:-diabagatekelly/catering-ledger}"
REGION="${REGION:-us-central1}"
POOL_ID="${POOL_ID:-github-pool}"
PROVIDER_ID="${PROVIDER_ID:-github-provider}"
DEPLOYER_SA_NAME="${DEPLOYER_SA_NAME:-gh-deployer}"
# Runtime identity of the function — MUST already be Editor on the Sheet.
RUNTIME_SA_EMAIL="${RUNTIME_SA_EMAIL:-ledger-writer@${PROJECT_ID}.iam.gserviceaccount.com}"
SET_GH_SECRETS=0
[ "${1:-}" = "--set-gh-secrets" ] && SET_GH_SECRETS=1

if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: no project set. Run: gcloud config set project <id>" >&2
  exit 1
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
DEPLOYER_SA_EMAIL="${DEPLOYER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
# Gen2 builds run as the default compute SA unless overridden.
BUILD_SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

echo "== Project: $PROJECT_ID ($PROJECT_NUMBER) | repo: $GITHUB_REPO =="

# ---- 1. enable APIs ---------------------------------------------------------
echo "-- enabling APIs"
gcloud services enable \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com iam.googleapis.com sts.googleapis.com \
  --project "$PROJECT_ID"

# ---- 2. deployer service account -------------------------------------------
echo "-- deployer SA: $DEPLOYER_SA_EMAIL"
gcloud iam service-accounts describe "$DEPLOYER_SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$DEPLOYER_SA_NAME" --project "$PROJECT_ID" \
       --display-name="GitHub Actions deployer (catering-ledger)"

echo "-- granting deployer roles (least privilege for a Gen2 deploy)"
for role in \
  roles/run.admin \
  roles/cloudfunctions.developer \
  roles/artifactregistry.writer \
  roles/cloudbuild.builds.editor \
  roles/storage.admin ; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER_SA_EMAIL}" --role="$role" \
    --condition=None >/dev/null
done

# Deployer must be able to act AS the runtime SA (to set it as the function
# identity) and AS the build SA (to run the Cloud Build). Scoped to those SAs.
echo "-- scoping serviceAccountUser to runtime + build SAs"
for target in "$RUNTIME_SA_EMAIL" "$BUILD_SA_EMAIL"; do
  gcloud iam service-accounts add-iam-policy-binding "$target" \
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
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
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
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA_EMAIL" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}" >/dev/null

# ---- 4. runtime secret in Secret Manager -----------------------------------
if [ -z "${WHATSAPP_VERIFY_TOKEN:-}" ] && [ -f .env ]; then
  WHATSAPP_VERIFY_TOKEN="$(grep -E '^WHATSAPP_VERIFY_TOKEN=' .env | cut -d= -f2- || true)"
fi
if [ -z "${WHATSAPP_VERIFY_TOKEN:-}" ]; then
  read -rsp "WhatsApp verify token (stored in Secret Manager): " WHATSAPP_VERIFY_TOKEN; echo
fi

echo "-- Secret Manager: whatsapp-verify-token"
gcloud secrets describe whatsapp-verify-token --project="$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud secrets create whatsapp-verify-token --project="$PROJECT_ID" --replication-policy=automatic
printf '%s' "$WHATSAPP_VERIFY_TOKEN" \
  | gcloud secrets versions add whatsapp-verify-token --project="$PROJECT_ID" --data-file=-

echo "-- granting runtime SA read access to the secret"
gcloud secrets add-iam-policy-binding whatsapp-verify-token --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# ---- 5. output the repo secrets --------------------------------------------
WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
SHEET_ID_VALUE="${SHEET_ID:-}"
if [ -z "$SHEET_ID_VALUE" ] && [ -f .env ]; then
  SHEET_ID_VALUE="$(grep -E '^SHEET_ID=' .env | cut -d= -f2- || true)"
fi

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
