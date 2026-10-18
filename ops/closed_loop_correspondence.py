#!/usr/bin/env python3
"""Grade a frozen closed-loop public-data model against completed native segments.

Private facts enter only the independent actual ledger/comparison, after replay.
Order IDs/schedules, actual fills and private funding never enter the simulator.
"""
import argparse
from collections import defaultdict
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'python'),str(ROOT/'ops')]
from mininautilus.bridge import Engine
from mininautilus.depth_backtest import public_events,replay_depth
from live_correspondence import ledger,unique,strategy_parity


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_groups(simulated,state,rows,journal):
    """Compare all decision/side groups, including missing/extra simulated orders."""
    bars={r['request']['revision']:r['request']['bar']['open_ms'] for r in rows if r['kind']=='strategy'}
    actual_keys={};groups=defaultdict(lambda:dict(actual_lots=0,simulated_lots=0,actual_value=F(0),simulated_value=F(0)))
    for line in journal.read_text().splitlines():
        envelope=json.loads(json.loads(line)['payload']).get('Input')
        if not envelope or not isinstance(envelope['event'],dict):continue
        event=envelope['event'];targeted=event.get('SubmitTargeted');intent=targeted['intent'] if targeted else event.get('Submit')
        if intent and str(intent['id']) in state['orders']:
            actual_keys[intent['id']]=(str(bars[targeted['revision']]) if targeted else 'operational',intent['side'])
            groups[actual_keys[intent['id']]]
    for fill in state['fills'].values():
        g=groups[actual_keys[fill['order_id']]];g['actual_lots']+=fill['qty'];g['actual_value']+=fill['qty']*fill['price']
    simulated_orders={o['intent']['id']:o for o in simulated['orders']}
    for order in simulated['orders']:
        groups[('operational' if order['operational'] else str(order['signal_open_ms']),order['intent']['side'])]
    for fill in simulated['fills']:
        order=simulated_orders[fill['order_id']]
        g=groups[('operational' if order['operational'] else str(order['signal_open_ms']),fill['side'])]
        g['simulated_lots']+=fill['qty'];g['simulated_value']+=fill['qty']*fill['price']
    differences=[];price_errors=[]
    for key,g in groups.items():
        a,b=g['actual_lots'],g['simulated_lots']
        error=abs(g['simulated_value']/b-g['actual_value']/a)/(g['actual_value']/a)*10000 if a and b else None
        if error is not None:price_errors.append(error)
        differences.append(dict(stage=key[0],side=key[1],actual_lots=a,simulated_lots=b,lot_error=abs(a-b),vwap_error_bps=float(error) if error is not None else None))
    total=sum(g['actual_lots'] for g in groups.values())
    return dict(groups=differences,absolute_lot_error=sum(g['lot_error'] for g in differences),actual_lots=total,
                max_vwap_error_bps=float(max(price_errors)) if price_errors else None)


def verify_core_cash(state, actual, quantum, tolerance):
    # Core omits zero opening balances from JSON by its documented serde contract.
    gross=(F(state['cash'])-F(state.get('opening_cash',0)))*quantum
    if abs(gross-actual['realized'])>tolerance:
        raise ValueError('independent actual/Core gross cash discrepancy')


def analyze_segment(segment,manifest,binary):
    journal=segment.parent/f'journal-{int(segment.name.split("-")[1]):06}.jsonl'
    state=json.loads(subprocess.check_output([str(binary),'inspect',str(journal)],text=True))
    rows=[json.loads(line) for line in (segment/'observations.jsonl').read_text().splitlines()]
    actual_report=json.loads((segment/'run-report.json').read_text())
    if actual_report['failed'] or not actual_report['cleanup_verified']:raise ValueError('incomplete live segment')
    boundaries=[r for r in rows if r['kind']=='runtime_boundary' and r['phase']=='start']
    if len(boundaries)!=1 or not boundaries[0]['flatten']:raise ValueError('frozen timer/flatten boundary missing')
    stops=[r for r in rows if r['kind']=='runtime_boundary' and r['phase']=='stop']
    if len(stops)!=1 or stops[0]['failed'] or abs(stops[0]['at_ms']-boundaries[0]['planned_stop_ms'])>manifest['stop_timer_tolerance_ms']:
        raise ValueError('live stop did not follow frozen timer policy')
    with tempfile.TemporaryDirectory() as temporary:
        config=Path(temporary)/'config.json';config.write_text(json.dumps(state['config']))
        with Engine(sim=True,binary=binary,config=config,time_mode='historical',extra_args=('--external-venue',)) as engine:
            simulated=replay_depth(engine,public_events(rows),manifest)
    raw_fills=unique([f for r in rows if r['kind']=='http' and r['request'].get('type')=='userFillsByTime' for f in r['response']],lambda f:f['tid'])
    raw_funding=[f for r in rows if r['kind']=='http' and r['request'].get('type')=='userFunding' for f in r['response']]
    actual=ledger(raw_fills,raw_funding)
    quantum=F(manifest['tick'])*F(manifest['lot'])
    verify_core_cash(state,actual,quantum,F(manifest['cash_quantum']))
    if actual['position']!=state['position']*F(manifest['lot']):
        raise ValueError('independent actual/Core ledger discrepancy')
    for key,value in actual.items():
        if key in actual_report['ledger'] and abs(value-F(actual_report['ledger'][key]))>F(manifest['cash_quantum']):raise ValueError('actual report ledger mismatch')
    if {str(f['tid']+1) for f in raw_fills}!=set(state['fills']):raise ValueError('actual execution bijection missing')
    for row in raw_fills:
        fill=state['fills'][str(row['tid']+1)];order=state['orders'][str(fill['order_id'])]
        session=json.loads((segment/'session.json').read_text())
        if (F(row['sz'])!=fill['qty']*F(manifest['lot']) or F(row['px'])!=fill['price']*F(manifest['tick'])
            or row['cloid']!='0x'+session['session']+f"{fill['order_id']:016x}"
            or row['side']!=('B' if order['intent']['side']=='Buy' else 'A')):raise ValueError('actual execution identity/terms')
    parity=strategy_parity(rows)
    if parity['status']!='PASS':raise ValueError('independent signal mismatch')
    comparison=compare_groups(simulated,state,rows,journal)
    actions=[json.loads(l)['payload'] for l in (segment/'actions.jsonl').read_text().splitlines()]
    attempted={a['key'] for a in actions};nonces={a['nonce']:a['key'] for a in actions}
    filled_ack=set()
    for row in rows:
        if row['kind']=='http' and row['path']=='/exchange' and row['request'].get('type')=='order':
            key=nonces[row['request']['nonce']]
            statuses=row['response'].get('response',{}).get('data',{}).get('statuses',[])
            if len(statuses)==1 and 'filled' in statuses[0]:filled_ack.add(key)
    actual_uncovered=sum(f'order-{i}' in attempted and f'order-{i}' not in filled_ack for i in state['orders'])
    return dict(segment=segment.name,accounting='PASS',signals=parity,comparison=comparison,
                actual_orders=sum(f'order-{i}' in attempted for i in state['orders']),actual_nonfilled_ack_orders=actual_uncovered,actual_unique_fills=len(raw_fills),
                actual_ledger={k:str(v) for k,v in actual.items()},simulated=simulated,
                provenance={p.name:sha(p) for p in (journal,segment/'observations.jsonl',segment/'session.json',segment/'actions.jsonl',segment/'run-report.json')})


def analyze(roots,manifest,binary):
    segments=[analyze_segment(segment,manifest,binary) for root in roots for segment in sorted(root.glob('segment-*')) if (segment/'run-report.json').exists()]
    count=sum(s['actual_orders'] for s in segments)
    qty=sum(s['comparison']['actual_lots'] for s in segments)
    qty_error=F(sum(s['comparison']['absolute_lot_error'] for s in segments),max(1,qty))
    prices=[s['comparison']['max_vwap_error_bps'] for s in segments if s['comparison']['max_vwap_error_bps'] is not None]
    uncovered=sum(o['coverage']!='IMMEDIATE_CROSS' for s in segments for o in s['simulated']['orders'])
    actual_uncovered=sum(s['actual_nonfilled_ack_orders'] for s in segments)
    unknown=sum(len(s['simulated']['unknown_funding']) for s in segments)
    failures=sum(len(s['simulated']['failures']) for s in segments)
    failed=qty_error>F(manifest['max_filled_lot_error_fraction']) or (bool(prices) and max(prices)>manifest['max_vwap_error_bps']) or failures
    simulated_count=sum(len(s['simulated']['orders']) for s in segments)
    order_count_error=F(abs(simulated_count-count),max(1,count))
    failed=failed or order_count_error>F(manifest['max_order_count_error_fraction'])
    incomplete=count<manifest['minimum_orders'] or simulated_count<manifest['minimum_orders'] or not qty or uncovered or actual_uncovered or unknown or not segments
    return dict(schema=1,status='FAIL' if failed else 'INCONCLUSIVE' if incomplete else 'PASS_COVERED_CLASS',
                scope='per-segment 3/8 SMA plus explicitly scheduled operational flatten; immediate-cross class only, no general passive/impact/PCA acceptance',
                distinct_strategy_bar_times=len({signal['open_ms'] for s in segments for signal in s['simulated']['signals']}),
                actual_orders=count,simulated_orders=simulated_count,order_count_error_fraction=float(order_count_error),actual_lots=qty,filled_lot_error_fraction=float(qty_error),max_vwap_error_bps=max(prices) if prices else None,
                uncovered_simulated_orders=uncovered,actual_nonfilled_ack_orders=actual_uncovered,unknown_funding_settlements=unknown,core_or_model_failures=failures,
                backtest_binary_sha256=sha(binary),analyzer_source_sha256=sha(Path(__file__)),segments=segments)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('roots',nargs='+',type=Path)
    parser.add_argument('--manifest',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--binary',type=Path,default=ROOT/'target/release/mininautilus')
    args=parser.parse_args();manifest=json.loads(args.manifest.read_text())
    result=analyze(args.roots,manifest,args.binary);result['manifest_sha256']=sha(args.manifest)
    with args.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='segments'}))
    if result['status']=='FAIL':raise SystemExit(1)
