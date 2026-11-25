#!/usr/bin/env bash
# One-time Google Cloud setup for the career coach (safe to re-run: existing
# resources are left alone). Run from the repo root with gcloud logged in as a
# project owner:  bash deploy/setup-gcp.sh
set -uo pipefail

P=project-b2f8fd7f-3661-4447-8b6
N=107836625993
A=${GCLOUD_ACCOUNT:-tellyourjourney04@gmail.com}
R=asia-south1
S=career-coach-voice-agent
URL=https://$S-$N.$R.run.app
REPO=${GITHUB_REPO:-alokranjan04/career_coach_voice_agent}
BILLING=${BILLING_ACCOUNT:-012BBA-A3C0D9-6E8AB4}
RUNTIME=career-coach-runtime@$P.iam.gserviceaccount.com
SCHED=career-coach-scheduler@$P.iam.gserviceaccount.com
DEPLOYER=career-coach-deployer@$P.iam.gserviceaccount.com
g() { gcloud "$@" --project=$P --account=$A --quiet; }

g services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com monitoring.googleapis.com \
  identitytoolkit.googleapis.com iamcredentials.googleapis.com sts.googleapis.com billingbudgets.googleapis.com

# ---- secrets (values are added separately, never in this repo) ----
for s in SESSION_SECRET RAZORPAY_KEY_SECRET RAZORPAY_WEBHOOK_SECRET TWILIO_AUTH_TOKEN; do
  g secrets create $s --replication-policy=user-managed --locations=$R 2>/dev/null && echo "created secret $s"
  g secrets add-iam-policy-binding $s --member=serviceAccount:$RUNTIME --role=roles/secretmanager.secretAccessor >/dev/null
done
# The session-signing secret is random and never needs to be known by anyone.
if ! g secrets versions list SESSION_SECRET --format='value(name)' | grep -q .; then
  python -c "import secrets,sys; sys.stdout.write(secrets.token_urlsafe(48))" | g secrets versions add SESSION_SECRET --data-file=- >/dev/null
  echo "generated SESSION_SECRET"
fi

# ---- scheduled jobs: hourly balance checks, daily retention, weekly quality review ----
g iam service-accounts create career-coach-scheduler --display-name="Career coach scheduler" 2>/dev/null
for spec in "cc-hourly|0 * * * *|hourly" "cc-daily-retention|30 3 * * *|daily" "cc-weekly-quality|0 9 * * 1|weekly"; do
  IFS='|' read -r name cron job <<<"$spec"
  args=(--location=$R --schedule="$cron" --time-zone=Asia/Kolkata --uri=$URL/internal/cron/$job
        --http-method=POST --oidc-service-account-email=$SCHED --oidc-token-audience=$URL)
  g scheduler jobs create http $name "${args[@]}" 2>/dev/null || g scheduler jobs update http $name "${args[@]}" >/dev/null
  echo "scheduler job $name: $cron -> /internal/cron/$job"
done

# ---- GitHub Actions: keyless deploy via Workload Identity Federation ----
g iam service-accounts create career-coach-deployer --display-name="Career coach GitHub deployer" 2>/dev/null
for role in roles/run.sourceDeveloper roles/serviceusage.serviceUsageConsumer roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/storage.admin roles/logging.viewer; do
  g projects add-iam-policy-binding $P --member=serviceAccount:$DEPLOYER --role=$role --condition=None >/dev/null
done
for sa in $RUNTIME $N-compute@developer.gserviceaccount.com; do
  g iam service-accounts add-iam-policy-binding $sa --member=serviceAccount:$DEPLOYER --role=roles/iam.serviceAccountUser >/dev/null
done
# The regression test needs the Gemini key.
g secrets add-iam-policy-binding GEMINI_API_KEY --member=serviceAccount:$DEPLOYER --role=roles/secretmanager.secretAccessor >/dev/null
g iam workload-identity-pools providers create-oidc career-coach --location=global --workload-identity-pool=github-actions \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='$REPO'" 2>/dev/null && echo "created WIF provider for $REPO"
g iam service-accounts add-iam-policy-binding $DEPLOYER --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$N/locations/global/workloadIdentityPools/github-actions/attribute.repository/$REPO" >/dev/null

# ---- billing budget: emails billing admins at 50/90/100% of the monthly budget ----
gcloud billing budgets list --billing-account=$BILLING --account=$A --billing-project=$P --format='value(displayName)' | grep -q "Career coach monthly" || \
  gcloud billing budgets create --billing-account=$BILLING --account=$A --billing-project=$P --display-name="Career coach monthly" \
    --budget-amount=${MONTHLY_BUDGET:-5000INR} --filter-projects=projects/$P \
    --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 >/dev/null && echo "budget ready"
echo "setup done"
