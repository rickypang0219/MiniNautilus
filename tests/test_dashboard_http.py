"""Real engine/observer processes: persistence, bounded pages, and restart catch-up."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from mininautilus import Engine


class DashboardHTTPTests(unittest.TestCase):
    def test_ten_hour_gap_and_observer_restart_rebuild_all_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            server = None

            def start():
                return subprocess.Popen([str(ROOT/'target/debug/mininautilus'), 'dashboard', directory, '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            def stop():
                server.terminate(); server.wait(timeout=5); server.stderr.close()

            def read(route):
                with urllib.request.urlopen(url+route, timeout=3) as response:
                    return json.load(response)

            def wait_seq(expected):
                deadline = time.monotonic()+12
                while time.monotonic()<deadline:
                    try:
                        sessions = read('/api/sessions')['sessions']
                        if sessions:
                            view = read('/api/session/'+sessions[0]['id']+'?kind=events&limit=7')
                            if view.get('seq')==str(expected) and not view.get('catching_up'):
                                return sessions[0]['id'], view
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(.05)
                self.fail('observer did not catch up')

            with Engine(path) as engine:
                engine.send(0, {'Quote': {'bid':99,'ask':101}}, received_time_ms=1_800_000_000_000)
                server = start()
                try:
                    key, first = wait_seq(1)
                    self.assertEqual(first['ledger']['rows'][0]['received_time_ms'],1_800_000_000_000)
                    generation = first['generation']
                    # UI/observer absent, engine keeps running. A virtual 10-hour
                    # wall-clock gap tests semantics without a 10-hour sleep.
                    stop(); server = None
                    for i in range(2,102):
                        engine.send(i, {'Quote': {'bid':99,'ask':101}}, received_time_ms=1_800_036_000_000+i,
                                    event_time_ms=1_800_036_000_000-i, time_source='late exchange')
                    server = start(); key, latest = wait_seq(101)
                    self.assertEqual(latest['generation'],generation)
                    self.assertNotIn('events',latest)
                    self.assertEqual(len(latest['ledger']['rows']),7)
                    self.assertGreaterEqual(latest['ledger']['rows'][0]['received_time_ms']-first['ledger']['rows'][0]['received_time_ms'],36_000_000)
                    seqs=[]; cursor=None
                    while True:
                        query='?kind=events&limit=7&after=1&until=101'
                        if cursor:query+='&before='+cursor
                        page=read('/api/session/'+key+query)['ledger']
                        seqs.extend(int(row['seq']) for row in page['rows'])
                        cursor=page['next_cursor']
                        if not cursor:break
                    self.assertEqual(seqs,list(range(101,1,-1)))
                    self.assertEqual(len(set(seqs)),100)
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        read('/api/session/'+key+'?limit=999999')
                    self.assertEqual(error.exception.code,400)
                    error.exception.close()
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(url+'/api/sessions',method='POST'))
                    self.assertEqual(error.exception.code,405)
                    error.exception.close()
                    # Replay of the source journal must remain valid after observing.
                    engine.send(102,'Tick',received_time_ms=1_800_036_000_102)
                    wait_seq(102)
                finally:
                    if server is not None:stop()
            subprocess.run([str(ROOT/'target/debug/mininautilus'),'inspect',str(path)],stdout=subprocess.DEVNULL,check=True)

    def test_historical_bridge_does_not_stamp_backtests_with_todays_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'historical.jsonl'
            with Engine(path,paper=True,time_mode='historical') as engine:
                engine.send(0,{'Quote':{'bid':99,'ask':101}},event_time_ms=1_600_000_000_000)
                engine.send(1,'Tick')
            payloads=[json.loads(json.loads(line)['payload']) for line in path.read_text().splitlines()]
            self.assertEqual(payloads[1]['Input']['time']['event_time_ms'],1_600_000_000_000)
            self.assertNotIn('received_time_ms',payloads[1]['Input']['time'])
            self.assertNotIn('time',payloads[2]['Input'])
