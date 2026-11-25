#!/usr/bin/env bash
# Log-based metrics, alert policies, uptime check and dashboard for the career
# coach. Safe to re-run. Run from the repo root:  bash deploy/observability.sh
set -uo pipefail

P=project-b2f8fd7f-3661-4447-8b6
N=107836625993
A=${GCLOUD_ACCOUNT:-tellyourjourney04@gmail.com}
S=career-coach-voice-agent
HOST=$S-$N.asia-south1.run.app
EMAIL=${ALERT_EMAIL:-ranjanalok2032@gmail.com}
g() { gcloud "$@" --project=$P --account=$A --quiet; }
TMP=$(mktemp -d)
BASE="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$S\""

# ---- log-based metrics (the app logs JSON events: see agent/obs.py) ----
counter() {  # name, description, extra filter
  g logging metrics create "$1" --description="$2" --log-filter="$BASE AND $3" 2>/dev/null \
    || g logging metrics update "$1" --description="$2" --log-filter="$BASE AND $3" >/dev/null
}
counter cc_calls_ended "Calls ended" 'jsonPayload.event="call_ended"'
counter cc_calls_placed "Calls placed" 'jsonPayload.event="call_placed"'
counter cc_errors "Error-level log entries" 'severity>=ERROR'
counter cc_alerts "App alerts (low balance, spend cap, Gemini credits)" 'jsonPayload.event="alert"'
counter cc_payments "Day passes bought" 'jsonPayload.event="payment_captured"'
counter cc_otp_verified "Phone numbers verified" 'jsonPayload.event="otp_verified"'

distribution() {  # name, description, event, field, unit
  cat > "$TMP/$1.yaml" <<EOF
name: $1
description: $2
filter: '$BASE AND jsonPayload.event="$3"'
valueExtractor: EXTRACT(jsonPayload.$4)
metricDescriptor: {metricKind: DELTA, valueType: DISTRIBUTION, unit: "$5"}
bucketOptions: {exponentialBuckets: {numFiniteBuckets: 30, growthFactor: 1.4, scale: 0.1}}
EOF
  g logging metrics create "$1" --config-from-file="$TMP/$1.yaml" 2>/dev/null \
    || g logging metrics update "$1" --config-from-file="$TMP/$1.yaml" >/dev/null
}
distribution cc_call_cost_inr "Estimated cost per call (INR)" call_ended cost_inr "1"
distribution cc_call_duration_secs "Call duration (seconds)" call_ended duration_secs "s"
distribution cc_quality_overall "Quality score per call (1-5)" quality_scored overall "1"
echo "metrics ready"

# ---- email notification channel ----
CH=$(g beta monitoring channels list --filter="labels.email_address=\"$EMAIL\"" --format='value(name)' | head -1)
if [ -z "$CH" ]; then
  CH=$(g beta monitoring channels create --display-name="Disha admin email" --type=email \
        --channel-labels=email_address=$EMAIL --format='value(name)')
fi
echo "channel $CH"

# ---- alert policies ----
policy() {  # display name, metric, threshold, duration, doc
  cat > "$TMP/policy.json" <<EOF
{"displayName": "$1", "combiner": "OR", "notificationChannels": ["$CH"],
 "documentation": {"content": "$5", "mimeType": "text/markdown"},
 "alertStrategy": {"autoClose": "3600s"},
 "conditions": [{"displayName": "$1",
   "conditionThreshold": {"filter": "metric.type=\"logging.googleapis.com/user/$2\" AND resource.type=\"cloud_run_revision\"",
     "comparison": "COMPARISON_GT", "thresholdValue": $3, "duration": "$4",
     "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_SUM", "crossSeriesReducer": "REDUCE_SUM"}],
     "trigger": {"count": 1}}}]}
EOF
  existing=$(g alpha monitoring policies list --filter="displayName=\"$1\"" --format='value(name)' | head -1)
  if [ -n "$existing" ]; then g alpha monitoring policies update "$existing" --policy-from-file="$TMP/policy.json" >/dev/null
  else g alpha monitoring policies create --policy-from-file="$TMP/policy.json" >/dev/null; fi
  echo "policy: $1"
}
policy "Disha: errors" cc_errors 2 0s "More than 2 errors in 5 minutes. Open Logs Explorer, filter severity>=ERROR for service $S."
policy "Disha: app alert" cc_alerts 0 0s "The app raised an alert (low Vobiz balance, daily spend cap, Gemini credits or budget). The email from the app has details."
policy "Disha: spend spike" cc_calls_placed 30 0s "More than 30 calls placed in 5 minutes: possible abuse. Check Logs and the daily spend cap."

# ---- uptime check on /health, with an alert ----
UP=$(g monitoring uptime list-configs --filter="displayName=\"Disha health\"" --format='value(name)' | head -1)
if [ -z "$UP" ]; then
  MSYS2_ARG_CONV_EXCL="--path=" g monitoring uptime create "Disha health" --resource-type=uptime-url --resource-labels=host=$HOST,project_id=$P \
    --path=/health --protocol=https --period=5 --timeout=10 >/dev/null
  UP=$(g monitoring uptime list-configs --filter="displayName=\"Disha health\"" --format='value(name)' | head -1)
fi
CHECK_ID=${UP##*/}
cat > "$TMP/uptime.json" <<EOF
{"displayName": "Disha: site down", "combiner": "OR", "notificationChannels": ["$CH"],
 "documentation": {"content": "The /health check failed from several regions. Check the Cloud Run service $S.", "mimeType": "text/markdown"},
 "conditions": [{"displayName": "Uptime check failing",
   "conditionThreshold": {"filter": "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"$CHECK_ID\"",
     "comparison": "COMPARISON_GT", "thresholdValue": 1, "duration": "300s",
     "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_NEXT_OLDER", "crossSeriesReducer": "REDUCE_COUNT_FALSE", "groupByFields": ["resource.label.*"]}],
     "trigger": {"count": 1}}}]}
EOF
existing=$(g alpha monitoring policies list --filter='displayName="Disha: site down"' --format='value(name)' | head -1)
if [ -z "$existing" ]; then g alpha monitoring policies create --policy-from-file="$TMP/uptime.json" >/dev/null; fi
echo "uptime check ready"

# ---- dashboard ----
chart() {  # title, metric, aligner
  echo "{\"title\": \"$1\", \"xyChart\": {\"dataSets\": [{\"plotType\": \"LINE\", \"timeSeriesQuery\": {\"timeSeriesFilter\": {
    \"filter\": \"metric.type=\\\"logging.googleapis.com/user/$2\\\" AND resource.type=\\\"cloud_run_revision\\\"\",
    \"aggregation\": {\"alignmentPeriod\": \"3600s\", \"perSeriesAligner\": \"$3\", \"crossSeriesReducer\": \"REDUCE_SUM\"}}}}]}}"
}
dist() {
  echo "{\"title\": \"$1\", \"xyChart\": {\"dataSets\": [{\"plotType\": \"LINE\", \"timeSeriesQuery\": {\"timeSeriesFilter\": {
    \"filter\": \"metric.type=\\\"logging.googleapis.com/user/$2\\\" AND resource.type=\\\"cloud_run_revision\\\"\",
    \"aggregation\": {\"alignmentPeriod\": \"3600s\", \"perSeriesAligner\": \"ALIGN_DELTA\", \"crossSeriesReducer\": \"REDUCE_MEAN\"}}}}]}}"
}
cat > "$TMP/dash.json" <<EOF
{"displayName": "Disha career coach", "gridLayout": {"columns": "2", "widgets": [
 $(chart "Calls placed per hour" cc_calls_placed ALIGN_SUM),
 $(chart "Calls ended per hour" cc_calls_ended ALIGN_SUM),
 $(dist "Average cost per call (INR)" cc_call_cost_inr),
 $(dist "Average call duration (s)" cc_call_duration_secs),
 $(dist "Average quality score (1-5)" cc_quality_overall),
 $(chart "Day passes bought" cc_payments ALIGN_SUM),
 $(chart "Errors" cc_errors ALIGN_SUM),
 $(chart "App alerts" cc_alerts ALIGN_SUM)]}}
EOF
DASH=$(g monitoring dashboards list --filter='displayName="Disha career coach"' --format='value(name)' | head -1)
if [ -z "$DASH" ]; then g monitoring dashboards create --config-from-file="$TMP/dash.json" >/dev/null; echo "dashboard created"; fi
rm -rf "$TMP"
echo "observability done: https://console.cloud.google.com/monitoring/dashboards?project=$P"
