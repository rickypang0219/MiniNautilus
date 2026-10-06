import sys
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'python'), str(Path(__file__).resolve().parents[1] / 'examples')]
from mininautilus.sma import Candle, CandleGap, SmaStrategy
from mininautilus.bridge import Engine
from sma_spot import audit, closed_candle

class SmaTests(unittest.TestCase):
    def test_long_flat_cross_duplicate_and_gap(self):
        s = SmaStrategy(2, 3, 2, '1s', long_only=True)
        for i, price in enumerate([1, 2, 3]): s.add(Candle(i*1000, i*1000+999, price))
        self.assertEqual(s.target, 2)
        self.assertFalse(s.add(Candle(2000,2999,3)))
        s.add(Candle(3000,3999,1)); s.add(Candle(4000,4999,1))
        self.assertEqual(s.target,0)
        with self.assertRaises(CandleGap): s.add(Candle(6000,6999,2))
        self.assertFalse(s.ready)

    def test_only_final_bars(self):
        m = dict(e='kline', s='BTCUSDT', k=dict(i='1s', x=False,t=0,T=999,c='1'))
        self.assertIsNone(closed_candle(m,Decimal('0.01'),'BTCUSDT','1s'))
        m['k']['x']=True
        self.assertEqual(closed_candle(m,Decimal('0.01'),'BTCUSDT','1s').price,100)

    def test_rolling_sums_match_naive(self):
        s=SmaStrategy(3,7,2,'1s',long_only=True)
        values=[]
        for i in range(100):
            price=(i*17)%31+1;values.append(price);s.add(Candle(i*1000,i*1000+999,price))
            self.assertEqual(s.fast_sum,sum(values[-3:]))
            self.assertEqual(s.slow_sum,sum(values[-7:]))

    def test_python_signal_rust_buy_then_flat_and_stale_quote(self):
        with tempfile.TemporaryDirectory() as d, Engine(Path(d)/'events',paper=True) as e:
            s=SmaStrategy(1,2,2,'1s',long_only=True)
            s.add(Candle(0,999,100));s.add(Candle(1000,1999,110))
            e.send(1,{'Quote':dict(bid=100,ask=101)})
            intent=s.intent(e.state,2000);self.assertEqual(intent['side'],'Buy')
            e.send(2,{'Submit':intent});e.send(3,{'Trade':dict(taker='Sell',price=101,qty=2)})
            audit(e.state,2);self.assertEqual(e.state['position'],2)
            s.add(Candle(2000,2999,90))
            e.send(4,{'Quote':dict(bid=99,ask=100)})
            intent=s.intent(e.state,3000);self.assertEqual(intent['side'],'Sell')
            e.send(5,{'Submit':intent});e.send(6,{'Trade':dict(taker='Buy',price=99,qty=2)})
            audit(e.state,2);self.assertEqual(e.state['position'],0)
            e.send(7,'MarketUnavailable');self.assertIsNone(s.intent(e.state,3000))
            e.send(10000,{'QuoteObserved':dict(bid=99,ask=100,observed_at=1)})
            effects=e.send(10001,{'Submit':dict(id=3,side='Buy',qty=1,limit=100,based_on_seq=e.state['seq'],valid_until=11000)})
            self.assertFalse(any('SendOrder' in x for x in effects))

if __name__ == '__main__': unittest.main()
