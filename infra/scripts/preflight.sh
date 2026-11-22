#!/usr/bin/env bash
# Checks that must pass before the main stack takes over a domain's mail.
#
#   preflight.sh mx DOMAIN REGION      no MX record except SES's own
#   preflight.sh ruleset REGION NAME   no active SES receipt rule set except NAME
#
# The deploy workflow runs both before `tofu apply`; pull request CI runs the
# MX check. Either one failing stops the run before anything changes.
set -euo pipefail

check_mx() {
  local domain="$1" region="$2" ours="inbound-smtp.$2.amazonaws.com"
  local records others
  records=$(dig +short MX "$domain")
  others=$(awk -v ours="$ours" '{ sub(/\.$/, "", $2) } $2 != ours' <<<"$records")
  if [[ -n "$others" ]]; then
    echo "::error::$domain already has MX records that would compete with SES:" >&2
    echo "$others" >&2
    echo "Remove them in Cloudflare (or move that mail elsewhere) first." >&2
    exit 1
  fi
  echo "ok: $domain has no competing MX records${records:+ (the SES record is present)}"
}

check_ruleset() {
  local region="$1" name="$2" active
  active=$(aws ses describe-active-receipt-rule-set --region "$region" \
    --query 'Metadata.Name' --output text 2>/dev/null || true)
  if [[ -n "$active" && "$active" != "None" && "$active" != "$name" ]]; then
    echo "::error::SES rule set '$active' is active in $region." >&2
    echo "Applying would deactivate it. Move its rules into '$name' or pick another region." >&2
    exit 1
  fi
  echo "ok: no other SES receipt rule set is active in $region"
}

case "${1:-}" in
  mx) check_mx "${2:?domain}" "${3:?region}" ;;
  ruleset) check_ruleset "${2:?region}" "${3:?rule set name}" ;;
  *) echo "usage: preflight.sh mx DOMAIN REGION | ruleset REGION NAME" >&2; exit 2 ;;
esac
