"""Flat paper rotation compacts both mirrors without reusing IDs or balances."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mininautilus.bridge import Engine, next_order_id
BINARY = ROOT / "target/debug/mininautilus"


class PaperRotationTests(unittest.TestCase):
    def test_rotations_preserve_cash_and_id_floor_and_reset_history(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            journals = [d / "0.jsonl"]
            with Engine(journals[0], paper=True, binary=BINARY, time_mode="historical") as e:
                for session in range(8):
                    for side, price, taker in (("Buy", 100, "Sell"), ("Sell", 102, "Buy")):
                        at = e.state["now"] + 1
                        e.send(at, {"Quote": {"bid": price, "ask": price}})
                        oid = next_order_id(e.state)
                        e.send(at, {"Submit": {"id": oid, "side": side, "qty": 1,
                            "limit": price, "based_on_seq": e.state["seq"], "valid_until": at+100}})
                        e.send(at, {"Trade": {"taker": taker, "price": price, "qty": 1}})
                    self.assertEqual(e.state["position"], 0)
                    journals.append(d / f"{session+1}.jsonl")
                    e.rotate_paper(journals[-1])
                    self.assertEqual(e.state["cash"], (session+1)*2)
                    self.assertEqual(e.state["health"], "Healthy")
                    self.assertEqual(e.state["orders"], {})
                    self.assertEqual(e.state["fills"], {})
                    self.assertEqual(next_order_id(e.state), (session+1)*2+1)
                final = dict(e.state)
            self.assertEqual(json.loads(subprocess.check_output([str(BINARY), "inspect", str(journals[-1])])), final)
            for old in journals[:-1]:
                subprocess.check_call([str(BINARY), "inspect", str(old)], stdout=subprocess.DEVNULL)

    def test_open_or_nonflat_book_cannot_rotate(self):
        for filled in (False, True):
            with self.subTest(filled=filled), tempfile.TemporaryDirectory() as d:
                d = Path(d)
                with Engine(d / "old", paper=True, binary=BINARY, time_mode="historical") as e:
                    e.send(0, {"Quote": {"bid": 100, "ask": 100}})
                    e.send(0, {"Submit": {"id": 1, "side": "Buy", "qty": 1,
                        "limit": 100, "based_on_seq": e.state["seq"], "valid_until": 100}})
                    if filled:
                        e.send(0, {"Trade": {"taker": "Sell", "price": 100, "qty": 1}})
                    with self.assertRaises(RuntimeError):
                        e.rotate_paper(d / "new")
                self.assertFalse((d / "new").exists())
