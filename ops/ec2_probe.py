#!/usr/bin/env python3
"""Create one temporary EC2 probe with no ingress, collect public Testnet checks,
then terminate it and delete its security group. Requires boto3 and EC2/SSM
permissions. Uses an existing subnet/default VPC; creates no IAM roles or VPCs.
No exchange credentials are copied to the instance.
"""
import argparse
import base64
import json
from pathlib import Path
import re
import time
import uuid


def main():
    import boto3
    from botocore.config import Config
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--region', default='ap-northeast-1')
    p.add_argument('--profile')
    p.add_argument('--subnet')
    p.add_argument('--probe-script', type=Path, default=Path(__file__).with_name('probe_testnet.py'))
    p.add_argument('--run-dir', type=Path, required=True)
    a = p.parse_args()
    if not re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-\d', a.region):
        p.error('invalid AWS region')
    probe = base64.b64encode(a.probe_script.read_bytes()).decode()
    a.run_dir.mkdir(parents=True, exist_ok=False)
    session = boto3.Session(profile_name=a.profile, region_name=a.region)
    client_config = Config(connect_timeout=8, read_timeout=20, retries={'max_attempts': 2})
    ec2 = session.client('ec2', config=client_config)
    identity = session.client('sts', config=client_config).get_caller_identity()
    # Persist account identity locally for audit; never print credentials.
    (a.run_dir/'identity.json').write_text(json.dumps(identity, default=str, indent=2))
    if a.subnet:
        subnet = ec2.describe_subnets(SubnetIds=[a.subnet])['Subnets'][0]
    else:
        subnets = ec2.describe_subnets(Filters=[{'Name':'default-for-az','Values':['true']}])['Subnets']
        if not subnets:
            raise RuntimeError('No default subnet: provide --subnet for an existing public subnet')
        subnet = sorted(subnets, key=lambda s:s['SubnetId'])[0]
    ami = session.client('ssm', config=client_config).get_parameter(
        Name='/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64')['Parameter']['Value']
    token = 'mininautilus-probe-' + uuid.uuid4().hex[:12]
    tags = [{'Key':'Name','Value':token}, {'Key':'Purpose','Value':'public-testnet-reachability'},
            {'Key':'DeleteAfterEpoch','Value':str(int(time.time()+900))}]
    metadata = {'region':a.region, 'subnet':subnet['SubnetId'], 'ami':ami, 'instance':None,
                'security_group':None, 'terminated':False, 'security_group_deleted':False}
    def save():
        (a.run_dir/'resources.json').write_text(json.dumps(metadata,indent=2))
    user_data = f'''#!/bin/bash
exec > >(tee /dev/console) 2>&1
# Independent cleanup even if the controller loses access.
shutdown -h +8
trap 'shutdown -h now' EXIT
mkdir -p /opt/mini-probe
printf '%s' '{probe}' | base64 -d > /opt/mini-probe/probe.py
cat > /opt/mini-probe/run.sh <<'RUN'
#!/bin/bash
set -eu
dnf -q -y install python3-pip
python3 -m pip -q install --target /opt/mini-probe/deps 'websockets==12.0'
PYTHONPATH=/opt/mini-probe/deps python3 /opt/mini-probe/probe.py --location '{a.region}' --samples 5 --output /opt/mini-probe/result.json || true
python3 -c "import base64; print('MINI_PROBE_JSON=' + base64.b64encode(open('/opt/mini-probe/result.json','rb').read()).decode(), flush=True)"
RUN
timeout 360 bash /opt/mini-probe/run.sh
'''
    found = False
    try:
        metadata['security_group'] = ec2.create_security_group(GroupName=token,
            Description='Temporary public Testnet probe; no ingress', VpcId=subnet['VpcId'],
            TagSpecifications=[{'ResourceType':'security-group','Tags':tags}])['GroupId']
        save()
        # New security groups have no ingress rules. Outbound access permits the
        # OS package repository plus the public Binance test hosts.
        instance = ec2.run_instances(ImageId=ami, InstanceType='t3.micro', MinCount=1, MaxCount=1,
            ClientToken=token, CreditSpecification={'CpuCredits':'standard'},
            InstanceInitiatedShutdownBehavior='terminate',
            MetadataOptions={'HttpEndpoint':'disabled'},
            NetworkInterfaces=[{'DeviceIndex':0,'SubnetId':subnet['SubnetId'],
                'Groups':[metadata['security_group']], 'AssociatePublicIpAddress':True}],
            BlockDeviceMappings=[{'DeviceName':'/dev/xvda','Ebs':{'VolumeSize':8,'VolumeType':'gp3',
                'Encrypted':True,'DeleteOnTermination':True}}],
            TagSpecifications=[{'ResourceType':kind,'Tags':tags} for kind in ('instance','volume')],
            UserData=user_data)['Instances'][0]
        metadata['instance'] = instance['InstanceId']
        save()
        print(json.dumps({'created':metadata['instance'],'region':a.region}), flush=True)
        deadline = time.monotonic()+600
        while time.monotonic() < deadline:
            time.sleep(10)
            try:
                result = ec2.get_console_output(InstanceId=metadata['instance'], Latest=True)
            except ec2.exceptions.ClientError as exc:
                if exc.response['Error']['Code'] in ('InvalidInstanceID.NotFound', 'IncorrectInstanceState'):
                    continue
                raise
            output = result.get('Output','')
            # AWS returns base64 console output; tolerate SDKs which decode it.
            if 'MINI_PROBE_JSON=' not in output:
                try:
                    output = base64.b64decode(output, validate=True).decode(errors='replace')
                except (ValueError, UnicodeError):
                    pass
            (a.run_dir/'console.txt').write_text(output)
            match = re.search(r'MINI_PROBE_JSON=([A-Za-z0-9+/=]+)', output)
            if match:
                report = json.loads(base64.b64decode(match[1]))
                report['aws_instance'] = metadata['instance']
                report['aws_region'] = a.region
                report['egress_location_verified'] = True
                (a.run_dir/'report.json').write_text(json.dumps(report, indent=2))
                found = True
                print(json.dumps({'public_gate_pass':report['public_gate_pass']}), flush=True)
                break
        if not found:
            raise RuntimeError('No probe result within 10 minutes; inspect console.txt')
    finally:
        # A timed-out RunInstances request may have succeeded server-side.
        # Discover only this invocation's unique idempotency token before cleanup.
        if metadata['security_group'] and not metadata['instance']:
            try:
                reservations = ec2.describe_instances(Filters=[
                    {'Name':'client-token','Values':[token]}])['Reservations']
                matches = [i for r in reservations for i in r['Instances']]
                if len(matches) == 1:
                    metadata['instance'] = matches[0]['InstanceId']
                    save()
            except ec2.exceptions.ClientError as exc:
                metadata['cleanup_discovery_error'] = exc.response['Error']['Code']
                save()
        if metadata['instance']:
            ec2.terminate_instances(InstanceIds=[metadata['instance']])
            ec2.get_waiter('instance_terminated').wait(InstanceIds=[metadata['instance']],
                WaiterConfig={'Delay':5,'MaxAttempts':60})
            metadata['terminated'] = True
            save()
        if metadata['security_group']:
            # ENI teardown may lag the terminated state briefly.
            for attempt in range(12):
                try:
                    ec2.delete_security_group(GroupId=metadata['security_group'])
                    metadata['security_group_deleted'] = True
                    save()
                    break
                except ec2.exceptions.ClientError as exc:
                    if exc.response['Error']['Code'] != 'DependencyViolation' or attempt == 11:
                        raise
                    time.sleep(5)
        print(json.dumps({'cleanup':metadata}), flush=True)


if __name__ == '__main__':
    main()
