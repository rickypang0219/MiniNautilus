#!/usr/bin/env python3
"""Optional isolated strategy for native-live: no exchange or engine client."""
import argparse
import json
import socket
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from mininautilus.sma import Candle, SmaStrategy


def run_strategy(socket_path):
    strategy = SmaStrategy(fast=3, slow=8, lots=20, interval='1m')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_path)
        with connection.makefile('rwb', buffering=0) as stream:
            while True:
                raw = stream.readline(8193)
                if not raw:
                    return
                if len(raw) > 8192 or not raw.endswith(b'\n'):
                    raise ValueError('strategy request cap')
                request = json.loads(raw)
                strategy.add(Candle(**request['bar']))
                answer = {key: request[key] for key in ('session', 'epoch', 'revision', 'deadline_ms')}
                answer['target'] = strategy.target if strategy.ready else None
                stream.write(json.dumps(answer, separators=(',', ':')).encode() + b'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    run_strategy(parser.parse_args().socket)
