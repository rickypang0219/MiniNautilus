#!/usr/bin/env python3
"""Task-owned EC2 worker. Credentials are read from SSM only into process memory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path("/opt/mini-native/repo")
STATE = Path("/mnt/native-state")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    parser.add_argument("phase",choices=["mount","rotation","crash","recover","inspect","cleanup"])
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
        if args.phase == "rotation":
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
            code = execute([native,"recover","--run-dir",str(directory),"--flatten"],"host-recovery",180)
            evidence.update(exit=code,recovery_seconds=time.monotonic()-started,after=account(True))
            if code:
                raise ValueError("original-session host recovery failed")
            state = json.loads(subprocess.check_output([str(ROOT/"target/release/mininautilus"),"inspect",str(directory/"journal.jsonl")],text=True))
            evidence["replayed"] = dict(position=state["position"],orders=len(state["orders"]),fills=len(state["fills"]))
            if state["position"] or len(state["orders"]) != 2 or len(state["fills"]) != 2:
                raise ValueError("unexpected duplicate/missing original or cleanup order")
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
