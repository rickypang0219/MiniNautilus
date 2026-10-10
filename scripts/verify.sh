#!/usr/bin/env sh
# Same gates as .github/workflows/tests.yml. Agents run this before claiming a change works.
set -eu
cd "$(dirname "$0")/.."
step() { printf '\n== %s\n' "$*"; "$@"; }
python3 -c "import websockets" 2>/dev/null || { echo "missing Python deps: pip install -r requirements.txt" >&2; exit 1; }
step cargo fmt --check
step cargo clippy --locked --all-targets -- -D warnings
step cargo test --locked
step cargo build --locked
step cargo build --locked --release --example compare_engine
mkdir -p runs
validation_dir=$(mktemp -d runs/verify.XXXXXX)
step python3 comparisons/run.py --platforms mini --seeds 20 --output "$validation_dir/comparison"
step python3 -m unittest discover -s tests -p 'test_*.py'
step node --test tests/dashboard_time.cjs
out=$(cargo run --locked --quiet -- demo)
printf '%s\n' "$out"
printf '%s\n' "$out" | grep -q 'position=2, unique_fills=1, reserved_buy=3, venue_orders=1, health=Healthy' \
  || { echo 'demo output changed' >&2; exit 1; }
echo 'VERIFY OK'
