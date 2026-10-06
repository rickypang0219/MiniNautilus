#!/usr/bin/env python3
"""Verify recorded Python SMA decisions and replay Rust's durable event journal."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from mininautilus.sma import SmaStrategy, Candle
from mininautilus.bridge import ROOT


def replay(path):
    metadata = json.loads((path / 'session.json').read_text())
    _, _, _, _, fast, slow, interval, lots, _ = metadata['parameters']
    strategy = SmaStrategy(fast, slow, lots, interval, long_only=True)
    count = 0
    for line in (path / 'observations.jsonl').read_text().splitlines():
        event = json.loads(line)
        if event['kind'] == 'strategy_reset': strategy.reset()
        elif event['kind'] == 'candle':
            strategy.add(Candle(event['open_ms'], event['close_ms'], event['price']))
            if strategy.signal() != event['signal']:
                raise ValueError('recorded signal differs from deterministic replay')
            count += 1
    state = json.loads(subprocess.check_output([str(ROOT / 'target/debug/mininautilus'), 'inspect', str(path / 'events.jsonl')]))
    summary = json.loads((path / 'summary.json').read_text())
    if state['position'] != summary['position_lots'] or len(state['fills']) != summary['fills']:
        raise ValueError('Rust replay differs from final summary')
    return dict(replayed_candles=count, position_lots=state['position'], fills=len(state['fills']),
                completed=summary.get('completed'), shutdown_reconciled=summary.get('shutdown_reconciled'))

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run_dir',type=Path)
    print(json.dumps(replay(p.parse_args().run_dir)))
