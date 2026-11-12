#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
php_bin=${PHP_BIN:-php}
while IFS= read -r -d '' file; do "$php_bin" -n -l "$file" >/dev/null; done < <(find . -name '*.php' -print0)
while IFS= read -r -d '' file; do node --check "$file"; done < <(find files -name '*.js' -print0)
"$php_bin" -n tests/policy.php
"$php_bin" -n tests/action.php
"$php_bin" -n tests/hook.php
"$php_bin" -n tests/schema.php
python3 -B tests/install.py
node tests/frontend.cjs
printf 'All offline checks passed. No CRM connections.\n'
