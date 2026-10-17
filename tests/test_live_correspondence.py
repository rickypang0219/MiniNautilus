import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("correspondence", Path(__file__).resolve().parents[1]/"ops/live_correspondence.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def fill(tid, side, size, price, start, pnl="0", fee="0.01"):
    return dict(tid=tid, oid=tid, time=tid, coin="BTC", side=side, sz=size,
                px=price, startPosition=start, closedPnl=pnl, fee=fee, feeToken="USDC")


def book(at, bid="99", ask="100", size="0.0002"):
    return dict(received_ms=at, message=dict(data=dict(coin="BTC", time=at,
                levels=[[dict(px=bid,sz=size)], [dict(px=ask,sz=size)]])))


class CorrespondenceTest(unittest.TestCase):
    def test_simulated_pnl_uses_predicted_fills_and_requires_mark_for_open_inventory(self):
        orders={1:dict(side="Buy"),2:dict(side="Sell")}
        predictions=[dict(id=1,status="MODELED",filled_lots=2,vwap="100"),dict(id=2,status="MODELED",filled_lots=2,vwap="110")]
        result=audit.scenario_pnl(predictions,orders,None,"0.001","1")
        self.assertEqual(result["net_ex_funding"],"979/50")
        self.assertEqual(audit.scenario_pnl(predictions[:1],orders,None,"0.001","1")["status"],"NOT_MEASURED")
    def test_independent_ledger_cross_zero_fee_rebate_funding_and_duplicates(self):
        rows = [fill(1,"B","2","100","0"),fill(2,"A","3","110","2","20"),
                fill(3,"B","1","105","-1","5",fee="-0.01")]
        funding = dict(time=2, hash="id", delta=dict(coin="BTC",type="funding",usdc="-0.25"))
        result = audit.ledger(rows+[rows[0]], [funding,funding])
        self.assertEqual(result["position"],0)
        self.assertEqual(result["cash_change"],audit.F("24.74"))
        for key, value in [("fee","0.02"),("px","101")]:
            changed = dict(rows[0],**{key:value})
            with self.assertRaisesRegex(ValueError,"changed duplicate"):
                audit.ledger(rows+[changed],[])
        broken = copy.deepcopy(rows); broken[1]["startPosition"]="1"
        with self.assertRaisesRegex(ValueError,"position gap"):
            audit.ledger(broken,[])

    def test_future_book_cannot_fill_past_order_and_depth_cannot_be_reused(self):
        manifest = dict(arrival_delay_ms=0,max_book_age_ms=100,tick="1",lot="0.00001")
        order = dict(id=1,at_ms=10,qty=20,side="Buy",limit=100)
        books = [book(10),book(20,ask="99")]
        result = audit.predict_orders([order,dict(order,id=2)],books,manifest)
        self.assertEqual(result[0]["vwap"],"100")
        self.assertEqual(result[1]["filled_lots"],0)
        self.assertEqual(audit.predict_orders([dict(order,at_ms=9)],books,manifest)[0]["status"],"NOT_MEASURED")
        self.assertEqual(audit.predict_orders([dict(order,at_ms=200)],books,manifest)[0]["status"],"NOT_MEASURED")
        changed = [books[0],book(20,ask="10000")]
        self.assertEqual(audit.predict_orders([order],books,manifest), audit.predict_orders([order],changed,manifest))

    def test_displayed_liquidity_is_partial_and_passive_is_not_invented(self):
        manifest = dict(arrival_delay_ms=0,max_book_age_ms=100,tick="1",lot="0.00001")
        order = dict(id=1,at_ms=10,qty=20,side="Buy",limit=100)
        self.assertEqual(audit.predict_orders([order],[book(10,size="0.0001")],manifest)[0]["filled_lots"],10)
        self.assertEqual(audit.predict_orders([dict(order,limit=99)],[book(10)],manifest)[0]["filled_lots"],0)

    def test_sma_replay_detects_wrong_target_without_using_reported_target(self):
        rows=[]
        for i in range(8):
            rows.append(dict(kind="strategy",received_ms=(i+1)*60000,
                request=dict(bar=dict(open_ms=i*60000,close_ms=(i+1)*60000-1,price=i+100)),
                answer=dict(target=20 if i==7 else None)))
        self.assertEqual(audit.strategy_parity(rows)["status"],"PASS")
        rows[-1]["answer"]["target"]=-20
        self.assertEqual(audit.strategy_parity(rows)["status"],"FAIL")
