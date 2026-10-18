#!/usr/bin/env python3
"""Bounded Tokyo rotation + retained-EBS replacement drill, with verified cleanup.

Use an already verified clean bundle and a JSON containing ami, subnet,
existing_sg, existing_profile and bucket. Never touches an existing user host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import time


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def begin_signed_phase(resources):
    # A flat proof belongs to the preceding phase, not future signed activity.
    resources.update(trading_started=True,flat_verified=False,potential_exposure=True)


def state_volume_safe(resources):
    return not resources.get("trading_started",False) or (
        resources.get("flat_verified") is True and resources.get("potential_exposure") is False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-receipt",type=Path,required=True)
    parser.add_argument("--config",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--verify-log",type=Path,required=True)
    parser.add_argument("--host-first",action="store_true",help="collect independent host-recovery evidence before the two live runs")
    parser.add_argument("--host-only",action="store_true",help="repeat retained-EBS recovery without the two rotation runs")
    parser.add_argument("--rotated-root",action="store_true",help="kill an exposed successor after rotation and recover its complete retained root")
    args=parser.parse_args()
    if args.rotated_root and not args.host_only:raise ValueError("rotated-root drill requires --host-only")
    if not args.verify_log.read_text().rstrip().endswith("VERIFY OK"):
        raise ValueError("full verify must finish before launch")
    build=json.loads(args.bundle_receipt.read_text());bundle=Path(build["path"])
    if sha(bundle)!=build["sha256"]:raise ValueError("bundle digest")
    config=json.loads(args.config.read_text());args.output.mkdir(parents=True,exist_ok=False)
    # The user selected this account via injected credentials; no local named profile exists.
    for key in ("AWS_PROFILE","AWS_DEFAULT_PROFILE"):os.environ.pop(key,None)
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    sdk=Config(connect_timeout=8,read_timeout=30,retries={"max_attempts":2})
    session=boto3.Session(region_name="ap-northeast-1")
    ec2=session.client("ec2",config=sdk);ssm=session.client("ssm",config=sdk)
    s3=session.client("s3",endpoint_url="https://s3.ap-northeast-1.amazonaws.com",config=Config(connect_timeout=8,read_timeout=60,retries={"max_attempts":2},s3={"addressing_style":"path"}))
    token="mini-lifecycle-"+str(int(time.time()));prefix="native-readiness-"+time.strftime('%Y%m%d',time.gmtime())+"/"+token
    parameter="/mininautilus/temporary/"+token
    resources=dict(token=token,region="ap-northeast-1",prefix=prefix,bucket=config["bucket"],instances=[],root_volumes=[],data_volume=None,build=build,cleanup_verified=False,flat_verified=False,verify_sha256=sha(args.verify_log),host_first=args.host_first,host_only=args.host_only,rotated_root=args.rotated_root,controller_sha256=sha(Path(__file__)),started=time.time())
    task=dict(bucket=config["bucket"],prefix=prefix,secret_parameter=parameter,bundle_sha256=build["sha256"])
    def save():
        (args.output/"resources.json").write_text(json.dumps(resources,indent=2))
    def log(message):print(json.dumps(dict(at=time.time(),message=message)),flush=True)
    def put(name,data):s3.put_object(Bucket=config["bucket"],Key=prefix+"/"+name,Body=data)
    def wait_for(predicate,seconds,message):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(3)
        raise TimeoutError(message)
    def state(instance):return ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
    def terminate(instance):
        if state(instance)!="terminated":ec2.terminate_instances(InstanceIds=[instance])
        wait_for(lambda:state(instance)=="terminated",180,"old signer termination")
        resources.setdefault("terminated",[]).append(instance);save()
    def command(instance,arguments,seconds):
        cmd="set -eu\nfor n in $(seq 1 90); do test -f /opt/mini-native/ready && break; sleep 3; done\ntest -f /opt/mini-native/ready\n/opt/mini-native/tools/bin/python /opt/mini-native/repo/ops/aws/native_drill_worker.py "+arguments
        response=ssm.send_command(InstanceIds=[instance],DocumentName="AWS-RunShellScript",Parameters={"commands":[cmd],"executionTimeout":[str(seconds)]},TimeoutSeconds=120)
        identity=response["Command"]["CommandId"];resources.setdefault("commands",[]).append(dict(instance=instance,id=identity,args=arguments));save()
        deadline=time.monotonic()+seconds+180
        while time.monotonic()<deadline:
            try:r=ssm.get_command_invocation(CommandId=identity,InstanceId=instance)
            except ClientError as error:
                if error.response["Error"]["Code"]!="InvocationDoesNotExist":raise
                time.sleep(3);continue
            if r["Status"] in ("Success","Cancelled","TimedOut","Failed"):
                (args.output/(identity+".json")).write_text(json.dumps(r,indent=2))
                if r["Status"]!="Success":raise RuntimeError("SSM phase failed: "+arguments)
                return
            time.sleep(5)
        raise TimeoutError("controller phase deadline: "+arguments)
    def launch(first):
        mappings=[dict(DeviceName="/dev/sda1",Ebs=dict(VolumeSize=12,VolumeType="gp3",Encrypted=True,DeleteOnTermination=True))]
        if first:mappings.append(dict(DeviceName="/dev/sdf",Ebs=dict(VolumeSize=8,VolumeType="gp3",Encrypted=True,DeleteOnTermination=False)))
        bootstrap=f'''#!/bin/bash
set -eu
exec > /var/log/mini-native-bootstrap.log 2>&1
shutdown -h +60
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv
mkdir -p /opt/mini-native/repo
python3 -m venv /opt/mini-native/tools
/opt/mini-native/tools/bin/pip install boto3
/opt/mini-native/tools/bin/python - <<'BOOT'
import boto3,hashlib,json,tarfile
from pathlib import Path
root=Path('/opt/mini-native');s3=boto3.client('s3',region_name='ap-northeast-1')
for name in ['bundle.tar.gz','task.json']:s3.download_file('{config["bucket"]}','{prefix}/'+name,str(root/name))
assert hashlib.sha256((root/'bundle.tar.gz').read_bytes()).hexdigest()=='{build["sha256"]}'
with tarfile.open(root/'bundle.tar.gz') as archive:archive.extractall(root/'repo',filter='data')
(root/'ready').write_text('verified')
BOOT
'''
        tags=[dict(Key="Name",Value=token),dict(Key="Purpose",Value="temporary-native-lifecycle-drill")]
        item=ec2.run_instances(ImageId=config["ami"],InstanceType="c7i.large",MinCount=1,MaxCount=1,
            ClientToken=token+("-first" if first else "-replacement"),IamInstanceProfile={"Arn":config["existing_profile"]},
            InstanceInitiatedShutdownBehavior="terminate",MetadataOptions=dict(HttpEndpoint="enabled",HttpTokens="required",HttpPutResponseHopLimit=1),
            NetworkInterfaces=[dict(DeviceIndex=0,SubnetId=config["subnet"],Groups=[config["existing_sg"]],AssociatePublicIpAddress=True)],
            BlockDeviceMappings=mappings,TagSpecifications=[dict(ResourceType=kind,Tags=tags) for kind in ("instance","volume")],UserData=bootstrap)["Instances"][0]
        identity=item["InstanceId"];resources["instances"].append(identity)
        for mapping in item.get("BlockDeviceMappings",[]):
            if mapping["DeviceName"]=="/dev/sdf":resources["data_volume"]=mapping["Ebs"]["VolumeId"]
            else:resources["root_volumes"].append(mapping["Ebs"]["VolumeId"])
        save();log("created task host "+identity)
        def record_volumes():
            current=ec2.describe_instances(InstanceIds=[identity])["Reservations"][0]["Instances"][0]
            found=current.get("BlockDeviceMappings",[])
            for mapping in found:
                volume=mapping["Ebs"]["VolumeId"]
                if mapping["DeviceName"]=="/dev/sdf" and first:resources["data_volume"]=volume
                elif volume not in resources["root_volumes"]:resources["root_volumes"].append(volume)
            save()
            return bool(found) and (not first or resources["data_volume"] is not None)
        wait_for(record_volumes,30,"record task volume IDs before bootstrap")
        wait_for(lambda:any(r["PingStatus"]=="Online" for r in ssm.describe_instance_information(Filters=[dict(Key="InstanceIds",Values=[identity])])["InstanceInformationList"]),360,"SSM bootstrap")
        item=ec2.describe_instances(InstanceIds=[identity])["Reservations"][0]["Instances"][0]
        for mapping in item["BlockDeviceMappings"]:
            volume=mapping["Ebs"]["VolumeId"]
            if mapping["DeviceName"]=="/dev/sdf" and first:resources["data_volume"]=volume
            elif volume not in resources["root_volumes"]:resources["root_volumes"].append(volume)
        save()
        return identity
    def signal_stop(signum,frame):raise KeyboardInterrupt("controller interrupted")
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,signal_stop)
    active=None;mounted=False
    try:
        values={name:os.environ[name] for name in ("HL_NETWORK","HL_MAIN_ADDRESS","HL_API_ADDRESS","HL_API_PRIVATE_KEY")}
        if values["HL_NETWORK"]!="testnet":raise ValueError("Testnet only")
        ssm.put_parameter(Name=parameter,Type="SecureString",Value=json.dumps(values),Overwrite=False,Tags=[dict(Key="Purpose",Value="temporary-native-lifecycle-drill")]);del values
        resources["secret_created"]=True;save()
        s3.upload_file(str(bundle),config["bucket"],prefix+"/bundle.tar.gz");put("task.json",json.dumps(task).encode())
        active=launch(True)
        # The exact IDs returned by this launch are persisted before any wait.
        volume=resources["data_volume"]
        if not volume:
            item=ec2.describe_instances(InstanceIds=[active])["Reservations"][0]["Instances"][0]
            for mapping in item["BlockDeviceMappings"]:
                if mapping["DeviceName"]=="/dev/sdf":resources["data_volume"]=mapping["Ebs"]["VolumeId"]
                elif mapping["Ebs"]["VolumeId"] not in resources["root_volumes"]:resources["root_volumes"].append(mapping["Ebs"]["VolumeId"])
            save();volume=resources["data_volume"]
        command(active,"mount --volume "+volume+" --initialize",120);mounted=True
        if args.rotated_root:command(active,"fixture",60)
        begin_signed_phase(resources);save()
        if not args.host_first and not args.host_only:command(active,"rotation",2400)
        command(active,"crash-rotated" if args.rotated_root else "crash",420);resources["potential_exposure"]=True;save()
        loss_started=time.monotonic();resources["host_loss_started_epoch"]=time.time();save()
        old=active;terminate(old);active=None;mounted=False
        # No replacement may start until EC2 proves the old signer is terminated.
        resources["fence"]=dict(instance=old,state=state(old),verified_epoch=time.time());save()
        put("fencing-receipt.json",json.dumps(resources["fence"]).encode())
        wait_for(lambda:ec2.describe_volumes(VolumeIds=[volume])["Volumes"][0]["State"]=="available",120,"retained EBS detach")
        active=launch(False)
        ec2.attach_volume(VolumeId=volume,InstanceId=active,Device="/dev/sdf")
        wait_for(lambda:ec2.describe_volumes(VolumeIds=[volume])["Volumes"][0]["State"]=="in-use",120,"retained EBS attach")
        command(active,"mount --volume "+volume,120);mounted=True
        command(active,"recover-rotated" if args.rotated_root else "recover",240)
        resources["host_loss_to_recovered_seconds"]=time.monotonic()-loss_started
        resources["flat_verified"]=True;resources["potential_exposure"]=False;save()
        resources["status"]="PASS" if resources["host_loss_to_recovered_seconds"]<=300 else "FAIL_RTO"
        if args.host_first and not args.host_only:
            begin_signed_phase(resources);save()
            command(active,"rotation",2400)
    except BaseException as error:
        resources["status"]="FAIL";resources["failure"]=type(error).__name__+": "+str(error);save()
        raise
    finally:
        if active and mounted:
            try:
                command(active,"cleanup",600);resources["flat_verified"]=True;resources["potential_exposure"]=False
            except BaseException as error:resources["cleanup_failure"]=type(error).__name__+": "+str(error)
        # Preserve a state volume if original-account cleanup cannot be proven.
        for identity in resources["instances"]:
            try:terminate(identity)
            except BaseException as error:resources.setdefault("termination_failures",[]).append(dict(instance=identity,error=type(error).__name__))
        volume_safe=state_volume_safe(resources)
        # Discover only this unique task's tagged volumes, including failed launches
        # whose RunInstances response did not yet expose block-device IDs.
        discovered=ec2.describe_volumes(Filters=[dict(Name="tag:Name",Values=[token]),dict(Name="tag:Purpose",Values=["temporary-native-lifecycle-drill"])])["Volumes"]
        resources["discovered_task_volumes"]=[v["VolumeId"] for v in discovered]
        volumes=resources["root_volumes"]+([resources["data_volume"]] if resources["data_volume"] and volume_safe else [])
        if volume_safe:volumes=list(dict.fromkeys(volumes+resources["discovered_task_volumes"]))
        resources["volume_cleanup"]={}
        for volume in volumes:
            try:
                item=ec2.describe_volumes(VolumeIds=[volume])["Volumes"][0]
                if item["State"]=="available":ec2.delete_volume(VolumeId=volume)
                def deleted():
                    try:ec2.describe_volumes(VolumeIds=[volume]);return False
                    except ClientError as error:
                        if error.response["Error"]["Code"]=="InvalidVolume.NotFound":return True
                        raise
                wait_for(deleted,90,"task volume deletion");resources["volume_cleanup"][volume]="deleted"
            except ClientError as error:
                if error.response["Error"]["Code"]=="InvalidVolume.NotFound":resources["volume_cleanup"][volume]="deleted"
                else:resources["volume_cleanup"][volume]=error.response["Error"]["Code"]
        if resources.get("secret_created"):
            ssm.delete_parameter(Name=parameter);resources["secret_deleted"]=True
        resources["cleanup_verified"]=all(state(i)=="terminated" for i in resources["instances"]) and volume_safe and all(v=="deleted" for v in resources["volume_cleanup"].values())
        resources["finished_epoch"]=time.time();save()
        put("controller-result.json",json.dumps(resources,indent=2).encode())
        log(dict(status=resources.get("status"),cleanup_verified=resources["cleanup_verified"],resources_file=str(args.output/"resources.json")))


if __name__=="__main__":main()
