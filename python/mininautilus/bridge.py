"""JSON-lines IPC keeps Python outside Rust's state ownership.

This reference bridge serializes requests, and is not an HFT transport.
Only the main runtime thread may call Engine; strategies receive snapshots.
"""
import json
import selectors
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Engine:
    def __init__(self, journal, *, paper=False, recover=False, config=None, binary=None):
        binary = Path(binary) if binary else ROOT / "target/debug/mininautilus"
        command = [str(binary), "paper" if paper else "serve", str(journal)]
        if recover:
            command.append("--recover")
        elif config:
            command.append(str(config))
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.state = self._read()["state"]
        except BaseException:
            self.close()
            raise

    def _read(self):
        if not self.selector.select(timeout=15):
            raise TimeoutError("Rust engine did not respond; do not dispatch or retry orders")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Rust engine stopped; recover its journal before continuing")
        return json.loads(line)

    def send(self, at, event):
        request = json.dumps({"at": at, "event": event}, separators=(",", ":"))
        self.process.stdin.write(request + "\n")
        self.process.stdin.flush()
        response = self._read()
        self.state = response["state"]
        return response["effects"]

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

