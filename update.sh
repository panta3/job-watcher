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
vercel deploy --prod --yes >/dev/null
URL="https://$SITE.vercel.app"

# 4. check it works, send the link to your phone
sleep 5
code=$(curl -s -o /dev/null -w '%{http_code}' "$URL")
echo "web page check: HTTP $code (200 = working)"
T=$(grep ntfy_topic "$HERE/infra/terraform.tfvars" | cut -d'"' -f2)
curl -s -H "Title: Job Watcher web page" -H "Click: $URL" -d "New address: $URL (asks for your password once)." "https://ntfy.sh/$T" >/dev/null

echo
echo "Updated. Your web page:  $URL"
echo "Logs: aws logs tail /aws/lambda/job-watcher --follow"
