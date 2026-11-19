#!/bin/bash
# Invoices every account whose billing day is today, or the accounts given.
set -euo pipefail
if [ $# -gt 0 ]; then
  accounts="$*"
else
  accounts=$(psql -XAtq <<<"select coalesce(string_agg(account, ' ' order by account), '') from accounts where billing_day = extract(day from now() at time zone 'UTC')::int;")
fi
if [ -z "$accounts" ]; then
  echo "$(date -u +%FT%TZ) no accounts due"
  exit 0
fi
n=$(psql -XAtq -v ON_ERROR_STOP=1 -v accounts="{${accounts// /,}}" <<<"select run_invoices(:'accounts'::text[]);")
echo "$(date -u +%FT%TZ) invoiced $n of: $accounts"
