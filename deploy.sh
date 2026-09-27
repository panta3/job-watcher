#!/usr/bin/env bash
# One-time AWS deploy: Lambda (scan every 15 min, digests at 8:05/21:05 Toronto time, web page link),
# S3 state, EventBridge Scheduler. Moves the watcher off this laptop without losing or re-sending anything.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/infra"
[ -f terraform.tfvars ] || { echo "infra/terraform.tfvars missing (needs ntfy_topic = \"...\")"; exit 1; }
grep -q '^ui_token' terraform.tfvars || echo "ui_token = \"$(openssl rand -hex 16)\"" >> terraform.tfvars

# 1. stop the laptop scanner first so the database we upload is the final local copy
crontab -l > "$HERE/crontab.before-deploy" 2>/dev/null || true
restore() { echo "Deploy failed: putting the laptop scanner back."; crontab "$HERE/crontab.before-deploy"; }
trap restore ERR
( grep -v 'job-watcher' "$HERE/crontab.before-deploy" || true ) | crontab -
echo "Waiting for any running scan to finish..."
flock "$HERE/jobs.db.scan.lock" true
python3 -c "import sqlite3; c=sqlite3.connect('$HERE/jobs.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"

# 2. infrastructure + state
terraform init -input=false >/dev/null
terraform apply -input=false -auto-approve -target=aws_s3_bucket.state -target=aws_s3_bucket_public_access_block.state
B="$(terraform output -raw bucket)"
aws s3 cp "$HERE/jobs.db" "s3://$B/jobs.db"
[ -f "$HERE/status.json" ] && aws s3 cp "$HERE/status.json" "s3://$B/status.json"
terraform apply -input=false -auto-approve
F="$(terraform output -raw function)"
aws lambda add-permission --function-name "$F" --statement-id FunctionURLInvokeFunction \
  --action lambda:InvokeFunction --principal "*" --invoked-via-function-url >/dev/null 2>&1 || true
trap - ERR

# 3. check it works, send the link to your phone
URL="$(terraform output -raw ui_url)"
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL")
echo "web page check: HTTP $code (200 = working)"
aws lambda invoke --function-name "$F" --payload '{"action":"test-notify"}' --cli-binary-format raw-in-base64-out /dev/null >/dev/null
T=$(grep ntfy_topic terraform.tfvars | cut -d'"' -f2)
curl -s -H "Title: Job Watcher web page" -H "Click: $URL" -d "Tap to open, then bookmark it / add to home screen." "https://ntfy.sh/$T" >/dev/null
pkill -f "watcher.py serve" 2>/dev/null || true   # the laptop page would go stale now

echo
echo "Deployed. Your web page (also sent to your phone):"
echo "  $URL"
echo "The laptop scanner is off; AWS runs it every 15 minutes now."
echo "Logs: aws logs tail /aws/lambda/job-watcher --follow"
