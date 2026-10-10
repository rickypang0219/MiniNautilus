#!/usr/bin/env sh
# Same gates as .github/workflows/tests.yml. Agents run this before claiming a change works.
set -eu
cd "$(dirname "$0")/.."
step() { printf '\n== %s\n' "$*"; "$@"; }
python3 -c "import websockets" 2>/dev/null || { echo "missing Python deps: pip install -r requirements.txt" >&2; exit 1; }
step cargo fmt --check
step cargo clippy --locked --all-targets -- -D warnings
step cargo test --locked
step python3 -m unittest discover -s tests -p 'test_*.py'
if command -v node >/dev/null; then step node --test tests/dashboard_time.cjs; fi
out=$(cargo run --locked --quiet -- demo)
printf '%s\n' "$out"
printf '%s\n' "$out" | grep -q 'position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy' \
  || { echo 'demo output changed' >&2; exit 1; }
echo 'VERIFY OK'
