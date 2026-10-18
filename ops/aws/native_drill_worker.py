#!/usr/bin/env python3
"""Task-owned EC2 worker. Credentials are read from SSM only into process memory."""
import argparse
import hashlib
import json
import os
from fractions import Fraction
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path("/opt/mini-native/repo")
STATE = Path("/mnt/native-state")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recovered_fill_proof(state, session, observations):
    """Match all original venue executions; one order may have many fills."""
    if session["instrument"].get("symbol")!="BTC" or session["instrument"].get("sz_decimals")!=5:
        raise ValueError("unexpected recovery proof instrument")
    if state["position"] or set(state["orders"])!={"1","2"}:
        raise ValueError("unexpected recovered position/orders")
    unique={}
    for fill in observations:
        tid=fill["tid"]
        if type(tid) is not int or tid<0 or (tid in unique and unique[tid]!=fill):
            raise ValueError("invalid/changed recovery execution")
        unique[tid]=fill
    if set(state["fills"])!={str(tid+1) for tid in unique}:
        raise ValueError("missing/extra recovery execution")
    totals={1:0,2:0};oids={}
    for tid,fill in unique.items():
        local=state["fills"][str(tid+1)];order_id=local["order_id"]
        if order_id not in totals:
            raise ValueError("unowned recovery execution")
        order=state["orders"][str(order_id)]
        if (local["execution_id"]!=tid+1 or local["qty"]<=0 or local["price"]<=0
            or Fraction(fill["sz"])!=Fraction(local["qty"],100000)
            or Fraction(fill["px"])!=Fraction(local["price"],10)
            or fill["coin"]!="BTC" or fill["cloid"]!="0x"+session["session"]+f"{order_id:016x}"
            or fill["side"]!=("B" if order["intent"]["side"]=="Buy" else "A")):
            raise ValueError("recovery execution identity/quantity/price mismatch")
        if type(fill["oid"]) is not int or fill["oid"]<=0 or oids.get(order_id,fill["oid"])!=fill["oid"]:
            raise ValueError("changed recovery order ID")
        oids[order_id]=fill["oid"];totals[order_id]+=local["qty"]
    for order_id,side in [(1,"Buy"),(2,"Sell")]:
        order=state["orders"][str(order_id)]
        if (order["intent"]["id"]!=order_id or order["intent"]["side"]!=side
            or not 0<totals[order_id]==order["filled"]<=order["intent"]["qty"]
            or order["lifecycle"] not in ("Filled","Canceled") or order["pending"] is not None or order["uncertain"]
            or (order["lifecycle"]=="Filled" and order["filled"]!=order["intent"]["qty"])):
            raise ValueError("recovery order not terminal/conserved")
    if totals[1]!=totals[2] or state["orders"]["2"]["intent"]["qty"]!=totals[1] or len(set(oids.values()))!=2:
        raise ValueError("recovery exposure/order identity mismatch")
    return dict(unique_fills=len(unique),filled_lots_by_order=totals,position=0)


def durable_tree(root):
    """Financial/control evidence only; observation logs are separately archived."""
    selected = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                and (p.name in ('current.json','policy.json','session.json','actions.jsonl','prepared.json','seal.json','run-report.json')
                     or p.name.startswith('journal-') or p.name.endswith('.cold'))]
    return {str(p.relative_to(root)):dict(bytes=p.stat().st_size,sha256=sha(p)) for p in selected}


def verify_tree(root, inventory, append_current):
    pointer=json.loads((root/'current.json').read_text()).get('payload',{}) if (root/'current.json').read_text().startswith('{') else {}
    active=f"journal-{pointer.get('index',1):06}.jsonl"
    actions=f"segment-{pointer.get('index',1):06}/actions.jsonl"
    for name, receipt in inventory.items():
        path=root/name
        if not path.is_file():raise ValueError('original durable file missing: '+name)
        data=path.read_bytes()
        if append_current and name in (active,actions):data=data[:receipt['bytes']]
        if len(data)!=receipt['bytes'] or hashlib.sha256(data).hexdigest()!=receipt['sha256']:
            raise ValueError('original durable bytes changed: '+name)


def session_fill_proof(state, session, observations):
    """Generic terminal session bijection, independent of fill cardinality."""
    if state['position'] or session['instrument']!={'symbol':'BTC','asset':session['instrument']['asset'],'sz_decimals':5}:
        raise ValueError('unsupported/nonflat session')
    facts={}
    for row in observations:
        tid=row['tid']
        if type(tid) is not int or tid<0 or (tid in facts and facts[tid]!=row):raise ValueError('changed execution')
        facts[tid]=row
    if set(state['fills'])!={str(t+1) for t in facts}:raise ValueError('missing/extra execution')
    totals={int(k):0 for k in state['orders']};oids={}
    for tid,row in facts.items():
        f=state['fills'][str(tid+1)];i=f['order_id'];o=state['orders'][str(i)]
        if (f['execution_id']!=tid+1 or f['qty']<=0 or f['price']<=0 or row['coin']!='BTC'
            or row['cloid']!='0x'+session['session']+f'{i:016x}'
            or row['side']!=('B' if o['intent']['side']=='Buy' else 'A')
            or Fraction(row['sz'])!=Fraction(f['qty'],100000) or Fraction(row['px'])!=Fraction(f['price'],10)
            or type(row['oid']) is not int or row['oid']<=0 or oids.get(i,row['oid'])!=row['oid']):raise ValueError('execution identity/terms')
        oids[i]=row['oid'];totals[i]+=f['qty']
    for key,o in state['orders'].items():
        i=int(key)
        if (o['intent']['id']!=i or totals[i]!=o['filled'] or not 0<=o['filled']<=o['intent']['qty']
            or o['lifecycle'] not in ('Filled','Canceled','Rejected') or o['pending'] is not None or o['uncertain']
            or (o['lifecycle']=='Filled' and o['filled']!=o['intent']['qty'])):raise ValueError('terminal quantity/state')
    if len(set(oids.values()))!=len(oids) or sum(totals[int(k)]*(1 if o['intent']['side']=='Buy' else -1) for k,o in state['orders'].items()):raise ValueError('identity/position conservation')
    return dict(unique_fills=len(facts),filled_lots_by_order=totals,position=0)


def crash_rotated(native):
    directory=STATE/'rotation-takeover'
    command=[native,'lifecycle','--run-dir',str(directory),'--seconds','900','--rotate-orders','4',
             '--rotate-seconds','60','--rotation-flatten','--flatten','--python-strategy']
    with (STATE/'rotated-active.log').open('x') as log:
        process=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        started=time.monotonic();found=None
        try:
            while process.poll() is None and time.monotonic()-started<300:
                pointer_path=directory/'current.json'
                if pointer_path.exists():
                    pointer=json.loads(pointer_path.read_text())['payload']
                    segment=directory/f"segment-{pointer['index']:06}"
                    if pointer['index']>=1 and pointer['phase']=='active' and (segment/'actions.jsonl').exists():
                        actual=account(False)
                        if actual['positions']:
                            found=dict(pointer=pointer,account=actual);break
                time.sleep(1)
            if found is None:raise ValueError('no active successor exposure within frozen deadline')
        finally:
            if process.poll() is None:
                import signal
                os.killpg(process.pid,signal.SIGKILL)
            code=process.wait()
        if code!=-9:raise ValueError('expected SIGKILL of active lifecycle')
    found.update(exit=code,killed_at_ms=time.time_ns()//1000000,durable_tree=durable_tree(directory))
    with (STATE/'rotated-crash-receipt.json').open('x') as stream:
        json.dump(found,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    subprocess.run(['sync','-f',str(STATE)],check=True)
    return found


def recover_rotated(native):
    directory=STATE/'rotation-takeover'
    receipt=json.loads((STATE/'rotated-crash-receipt.json').read_text())
    verify_tree(directory,receipt['durable_tree'],False)
    started=time.monotonic()
    code=execute([native,'recover-lifecycle','--run-dir',str(directory),'--flatten'],'rotated-host-recovery',180)
    if code:raise ValueError('original rotated root recovery failed')
    verify_tree(directory,receipt['durable_tree'],True)
    pointer=json.loads((directory/'current.json').read_text())['payload']
    segment=directory/f"segment-{pointer['index']:06}"
    state=json.loads(subprocess.check_output([str(ROOT/'target/release/mininautilus'),'inspect',str(segment/'journal.jsonl')],text=True))
    rows=[json.loads(line) for p in segment.glob('*observations*.jsonl') for line in p.read_text().splitlines()]
    fills=[f for r in rows if r['kind']=='http' and r['request'].get('type')=='userFillsByTime' for f in r['response']]
    funding=[f for r in rows if r['kind']=='http' and r['request'].get('type')=='userFunding' for f in r['response']]
    import sys
    sys.path.insert(0,str(ROOT/'ops'))
    from live_correspondence import ledger,decimal
    proof=session_fill_proof(state,json.loads((segment/'session.json').read_text()),fills)
    independent=ledger(fills,funding)
    report=json.loads(sorted(segment.glob('recovery-[0-9]*.json'))[-1].read_text())
    for key,value in independent.items():
        if key in report['ledger'] and abs(value-Fraction(report['ledger'][key]))>Fraction(1,10**6):raise ValueError('independent ledger discrepancy')
    return dict(exit=code,recovery_seconds=time.monotonic()-started,after=account(True),fill_proof=proof,
                independent_ledger={k:decimal(v) for k,v in independent.items()},
                original_files=len(receipt['durable_tree']),rotations=pointer['index'],original_prefixes_preserved=True)


def completed_root(directory):
    report_path=directory/"lifecycle-report.json"
    pointer_path=directory/"current.json"
    if not report_path.exists() or not pointer_path.exists():
        return False
    report=json.loads(report_path.read_text())
    pointer=json.loads(pointer_path.read_text())["payload"]
    segments=report["segments"]
    return (pointer["phase"]=="stopped" and report["position"]==0 and bool(segments)
            and all(s["cleanup_verified"] and not s["failed"] for s in segments))


def completed_diagnostic():
    path=STATE/"recover-result.json"
    if not path.exists():return False
    report=json.loads(path.read_text())
    return report.get("status")=="PASS" and report.get("exit")==0 and report["replayed"]["position"]==0 and not report["after"]["positions"] and not report["after"]["open_orders"]


def info(kind):
    request = urllib.request.Request("https://api.hyperliquid-testnet.xyz/info",
        data=json.dumps(dict(type=kind,user=os.environ["HL_MAIN_ADDRESS"])).encode(),
        headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=10) as response:
        return json.load(response)


def account(require_flat):
    state, orders = info("clearinghouseState"), info("openOrders")
    result = dict(at_ms=time.time_ns()//1000000,positions=state["assetPositions"],open_orders=orders)
    if require_flat and (result["positions"] or orders):
        raise ValueError("actual account is not flat/empty; recover original session")
    return result


def execute(command, name, timeout):
    with (STATE/(name+".log")).open("x") as log, (STATE/(name+"-resources.jsonl")).open("x") as samples:
        process = subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        started = time.monotonic()
        try:
            while process.poll() is None:
                pids, pending, rss = [], [process.pid], 0
                while pending:
                    pid = pending.pop()
                    try:
                        status = Path(f"/proc/{pid}/status").read_text()
                        memory = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:"))
                        pending.extend(int(n) for n in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
                        rss += memory
                        pids.append(dict(pid=pid,rss_kib=memory))
                    except (OSError,StopIteration):
                        pass
                samples.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(),elapsed=time.monotonic()-started,processes=pids,rss_sum_kib=rss))+"\n")
                samples.flush()
                if time.monotonic()-started > timeout:
                    raise TimeoutError(name)
                time.sleep(1)
            return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(90)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()


def archive(s3, task, name):
    path = Path("/tmp")/(name+".tar.gz")
    # Dereference journal aliases so downloaded evidence is relocatable.
    with tarfile.open(path,"x:gz",dereference=True) as tar:
        tar.add(STATE,arcname="state")
    s3.upload_file(str(path),task["bucket"],task["prefix"]+"/"+path.name)
    receipt = dict(key=task["prefix"]+"/"+path.name,sha256=sha(path),bytes=path.stat().st_size)
    s3.put_object(Bucket=task["bucket"],Key=task["prefix"]+"/"+name+"-archive.json",Body=json.dumps(receipt).encode())


def mount(volume, initialize):
    device = Path("/dev/disk/by-id")/("nvme-Amazon_Elastic_Block_Store_"+volume.replace("-",""))
    subprocess.run(["udevadm","settle"],check=True)
    if not device.exists():
        raise ValueError("expected task EBS serial not present")
    filesystem = subprocess.run(["blkid","-s","TYPE","-o","value",str(device)],capture_output=True,text=True)
    if initialize:
        if filesystem.returncode == 0:
            raise ValueError("refusing to format a nonempty task device")
        subprocess.run(["mkfs.ext4","-q",str(device)],check=True)
    elif filesystem.returncode or filesystem.stdout.strip() != "ext4":
        raise ValueError("recovery requires the original ext4 filesystem; never format")
    STATE.mkdir(exist_ok=True)
    subprocess.run(["mount","-o","nodev,nosuid",str(device),str(STATE)],check=True)
    subprocess.run(["findmnt","--target",str(STATE)],check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase",choices=["mount","rotation","crash","recover","crash-rotated","recover-rotated","fixture","inspect","cleanup"])
    parser.add_argument("--volume")
    parser.add_argument("--initialize",action="store_true")
    args = parser.parse_args()
    if args.phase == "mount":
        mount(args.volume,args.initialize)
        return
    import boto3
    task = json.loads(Path("/opt/mini-native/task.json").read_text())
    ssm = boto3.client("ssm",region_name="ap-northeast-1")
    s3 = boto3.client("s3",region_name="ap-northeast-1")
    credentials = json.loads(ssm.get_parameter(Name=task["secret_parameter"],WithDecryption=True)["Parameter"]["Value"])
    if credentials["HL_NETWORK"] != "testnet":
        raise ValueError("Testnet only")
    os.environ.update(credentials)
    del credentials
    os.environ["PYTHON"] = "/opt/mini-native/tools/bin/python"
    os.environ["PATH"] = "/opt/mini-native/tools/bin:"+os.environ["PATH"]
    os.chdir(ROOT)
    for name, digest in json.loads((ROOT/"BUILD.json").read_text())["files"].items():
        if sha(ROOT/name) != digest:
            raise ValueError("bundle identity mismatch: "+name)
    native = str(ROOT/"target/release/native-live")
    evidence = dict(phase=args.phase,started_ms=time.time_ns()//1000000)
    try:
        if args.phase == "fixture":
            code=execute([str(ROOT/'target/release/native-tests'),'--exact','native::venue::tests::eight_hundred_verified_terminal_facts_plus_four_active_query_only_four_statuses','--nocapture'],'terminal-cache-fixture',30)
            if code:raise ValueError('800 terminal + 4 active fixture failed')
            text=(STATE/'terminal-cache-fixture.log').read_text()
            metrics=[line.split('terminal_cache_fixture=',1)[1] for line in text.splitlines() if 'terminal_cache_fixture=' in line]
            if len(metrics)!=1:raise ValueError('fixture did not execute exactly once')
            evidence['metrics']=json.loads(metrics[0])
        elif args.phase == "crash-rotated":
            evidence['before']=account(True)
            evidence['crash']=crash_rotated(native)
        elif args.phase == "recover-rotated":
            evidence.update(recover_rotated(native))
        elif args.phase == "rotation":
            evidence["before"] = account(True)
            for index in (1,2):
                name = f"rotation-{index}"
                directory = STATE/name
                code = execute([native,"lifecycle","--run-dir",str(directory),"--seconds","900",
                    "--rotate-orders","4","--rotate-seconds","180","--rotation-flatten","--flatten","--python-strategy"],name,1200)
                evidence[name] = dict(exit=code)
                if code:
                    if (directory/"current.json").exists():
                        evidence[name]["recovery_exit"] = execute([native,"recover-lifecycle","--run-dir",str(directory),"--flatten"],name+"-recovery",180)
                    raise RuntimeError("signed lifecycle failed; original failure retained")
                report = json.loads((directory/"lifecycle-report.json").read_text())
                if report["rotations"] < 1 or report["position"] != 0 or report["elapsed_seconds"] < 900:
                    raise ValueError("rotation acceptance incomplete")
                evidence[name]["report"] = report
                evidence[name]["after"] = account(True)
                for segment in sorted(directory.glob("segment-*")):
                    if not (segment/"run-report.json").exists():
                        continue
                    output = segment/"correspondence.json"
                    code = execute([os.environ["PYTHON"],str(ROOT/"ops/live_correspondence.py"),str(segment),
                        "--manifest",str(ROOT/"comparisons/native-readiness/correspondence-v1.json"),"--output",str(output)],f"{name}-{segment.name}-audit",30)
                    if code:
                        raise ValueError("independent accounting/replay analyzer failed")
                archive(s3,task,name)
        elif args.phase == "crash":
            evidence["before"] = account(True)
            directory = STATE/"crash-original"
            code = execute([native,"diagnostic","--run-dir",str(directory),"--crash-after-fill"],"crash",120)
            evidence["exit"] = code
            if code != 86:
                raise ValueError("expected original diagnostic crash-after-fill exit 86")
            evidence["after_crash"] = account(False)
            evidence["durable_sha256"] = {name:sha(directory/name) for name in ("session.json","actions.jsonl","journal.jsonl")}
            with (STATE/"crash-receipt.json").open("x") as stream:
                json.dump(evidence,stream,indent=2);stream.flush();os.fsync(stream.fileno())
            # Record file metadata durably before testing host replacement.
            subprocess.run(["sync","-f",str(STATE)],check=True)
        elif args.phase == "recover":
            started = time.monotonic()
            directory = STATE/"crash-original"
            receipt = json.loads((STATE/"crash-receipt.json").read_text())
            if any(sha(directory/name) != digest for name,digest in receipt["durable_sha256"].items()):
                raise ValueError("durable bytes lost/changed across host replacement")
            attempts=[]
            for attempt in range(3):
                code=execute([native,"recover","--run-dir",str(directory),"--flatten"],f"host-recovery-{attempt}",60)
                attempts.append(code)
                if code==0:break
                time.sleep(1<<attempt)
            evidence["recovery_attempt_exits"]=attempts
            evidence.update(exit=code,recovery_seconds=time.monotonic()-started,after=account(True))
            if code:
                raise ValueError("original-session host recovery failed")
            state = json.loads(subprocess.check_output([str(ROOT/"target/release/mininautilus"),"inspect",str(directory/"journal.jsonl")],text=True))
            evidence["replayed"] = dict(position=state["position"],orders=len(state["orders"]),fills=len(state["fills"]))
            observed=[]
            for path in directory.glob("recovery-observations-*.jsonl"):
                for line in path.read_text().splitlines():
                    row=json.loads(line)
                    if row["kind"]=="http" and row["request"].get("type")=="userFillsByTime":
                        observed.extend(row["response"])
            evidence["fill_proof"]=recovered_fill_proof(state,json.loads((directory/"session.json").read_text()),observed)
        elif args.phase == "cleanup":
            for directory in sorted(STATE.glob("rotation-*")):
                if directory.is_dir() and (directory/"current.json").exists():
                    # Older verified-flat sessions must not absorb subsequent
                    # independent session trades into their original baselines.
                    if completed_root(directory):
                        continue
                    pointer=json.loads((directory/"current.json").read_text())["payload"]
                    segment=directory/f"segment-{pointer['index']:06}"
                    if (segment/"actions.jsonl").exists():
                        code=execute([native,"recover-lifecycle","--run-dir",str(directory),"--flatten"],directory.name+"-final-cleanup",180)
                        if code: raise ValueError("lifecycle cleanup unresolved")
            directory=STATE/"crash-original"
            if (directory/"session.json").exists() and not completed_diagnostic():
                code=execute([native,"recover","--run-dir",str(directory),"--flatten"],"crash-final-cleanup",180)
                if code: raise ValueError("original crash cleanup unresolved")
            evidence["account"]=account(True)
        else:
            evidence["account"] = account(False)
        evidence["status"] = "PASS"
    except BaseException as error:
        evidence["status"] = "FAIL"
        evidence["failure"] = type(error).__name__+": "+str(error)
        raise
    finally:
        evidence["finished_ms"] = time.time_ns()//1000000
        with (STATE/(args.phase+"-result.json")).open("x") as stream:
            json.dump(evidence,stream,indent=2)
        s3.put_object(Bucket=task["bucket"],Key=task["prefix"]+"/"+args.phase+"-result.json",Body=json.dumps(evidence).encode())
        archive(s3,task,args.phase+"-final")


if __name__ == "__main__":
    main()
