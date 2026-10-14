#!/usr/bin/env python3
"""Package a clean, verified native release for a short-lived EC2 experiment.

Only tracked repository files and the two explicit release binaries are included.
Credentials, .env files, runs, local Git configuration and virtualenvs are excluded.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import time

ROOT=Path(__file__).resolve().parents[1]


def bundle(output):
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():
        raise ValueError('commit reviewed source and freeze the harness before packaging')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    names=[name for name in tracked if name and not Path(name).name.startswith(".env")]
    names+=['target/release/native-live','target/release/mininautilus']
    files={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    receipt=dict(schema=1,commit=commit,dirty=False,files=files,
        rustc=subprocess.check_output(['rustc','-Vv'],text=True).strip(),
        build_commands=['cargo build --locked --release --bin mininautilus',
                        'cargo build --locked --release --features native-runtime --bin native-live'],
        created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    with output.open('xb') as stream, tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for name in names:
            archive.add(ROOT/name,arcname=name,recursive=False)
        data=(json.dumps(receipt,indent=2)+'\n').encode()
        info=tarfile.TarInfo('BUILD.json');info.size=len(data);info.mode=0o644
        archive.addfile(info,io.BytesIO(data))
    print(json.dumps(dict(path=str(output),commit=commit,sha256=hashlib.sha256(output.read_bytes()).hexdigest(),bytes=output.stat().st_size)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    bundle(parser.parse_args().output)
