#!/usr/bin/env bash
# Ship code changes to AWS and refresh the short web address. Safe to re-run: unlike deploy.sh it never
# touches the database in S3.
#   ./update.sh                 ship the current code
#   ./update.sh --new-address   also throw away the AWS address behind the short link and get a fresh one
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SITE="${JOBS_SITE_NAME:-aarav-jobs}"   # the page lives at https://$SITE.vercel.app
cd "$HERE/infra"

# 1. the page's password (replaces the old ?k= key in the link)
sed -i '/^ui_token/d' terraform.tfvars
if ! grep -q '^ui_password' terraform.tfvars; then
  while :; do
    read -rsp "Choose a password for the web page (8+ characters): " PW; echo
    [ "${#PW}" -ge 8 ] && [[ "$PW" != *'"'* && "$PW" != *'\'* ]] && break
    echo "Too short, or contains a quote/backslash. Try again."
  done
  echo "ui_password = \"$PW\"" >> terraform.tfvars
fi
# the business profile's password and phone-alert topic (see profiles.py); generated, printed at the end
grep -q '^biz_ui_password' terraform.tfvars || echo "biz_ui_password = \"$(openssl rand -base64 9 | tr -dc 'A-Za-z0-9' | head -c 10)\"" >> terraform.tfvars
grep -q '^biz_ntfy_topic' terraform.tfvars || echo "biz_ntfy_topic = \"jobs-biz-$(openssl rand -hex 8)\"" >> terraform.tfvars
[ "${1:-}" = "--password-only" ] && exit 0   # deploy.sh needs the password before its first apply

# 2. AWS
terraform init -input=false >/dev/null
REPLACE=()
[ "${1:-}" = "--new-address" ] && REPLACE=(-replace=aws_lambda_function_url.ui)
terraform apply -input=false -auto-approve "${REPLACE[@]}"
F="$(terraform output -raw function)"
aws lambda add-permission --function-name "$F" --statement-id FunctionURLInvokeFunction \
  --action lambda:InvokeFunction --principal "*" --invoked-via-function-url >/dev/null 2>&1 || true
AWS_URL="$(terraform output -raw ui_url)"

# 3. the short address: a free Vercel project that passes every request through to AWS
mkdir -p "$HERE/site"
cd "$HERE/site"
echo "{\"rewrites\": [{\"source\": \"/(.*)\", \"destination\": \"${AWS_URL%/}/\$1\"}]}" > vercel.json
[ -d .vercel ] || vercel link --yes --project "$SITE" >/dev/null
# never let GitHub pushes deploy: the repo has no rewrite (site/ is gitignored), so a push deploy is a 404 page
vercel git disconnect --yes >/dev/null 2>&1 || true
URL="https://$SITE.vercel.app"
# 4. check it works, then tell your phone
for try in 1 2 3; do
  vercel deploy --prod --yes >/dev/null || true
  sleep 5
  code=$(curl -s -o /dev/null -w '%{http_code}' "$URL")
  [ "$code" = 200 ] && break
  echo "web page check: HTTP $code, redeploying ($try/3)..."
done
[ "$code" = 200 ] || { echo "The web page is NOT working (HTTP $code). Run: cd site && vercel deploy --prod"; exit 1; }
echo "web page check: HTTP 200, working"
T=$(grep "^ntfy_topic" "$HERE/infra/terraform.tfvars" | cut -d'"' -f2)
[ ${#REPLACE[@]} -gt 0 ] && curl -s -H "Title: Job Watcher web page" -H "Click: $URL" \
  -d "New address: $URL (asks for your password once)." "https://ntfy.sh/$T" >/dev/null

echo
echo "Updated. Your web page:  $URL"
echo "Business profile: same address, password $(grep '^biz_ui_password' "$HERE/infra/terraform.tfvars" | cut -d'"' -f2),"
echo "  phone alerts: ntfy app -> subscribe to topic $(grep '^biz_ntfy_topic' "$HERE/infra/terraform.tfvars" | cut -d'"' -f2)"
echo "Logs: aws logs tail /aws/lambda/job-watcher --follow"
