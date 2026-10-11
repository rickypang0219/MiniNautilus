# Tokyo Testnet host

> Superseded by the CDK app in [`infra/`](../../infra/README.md) (Graviton t4g.micro, deployed 2026-10-11).

`testnet-host.json` is a CloudFormation **infrastructure** template, not an automatic trading deployment. It creates one AL2023 x86_64 t3.micro (2 burstable vCPU, 1 GiB RAM), encrypted 8 GiB gp3, a dedicated public subnet/VPC, Elastic IP, no inbound security-group rules, HTTPS-only outbound, an SSM instance role, and an S3 gateway endpoint. No exchange credentials, SSH keys, containers, NAT Gateway, ALB or paid interface endpoints are installed. Standard CPU credits avoid surplus-credit bills but sustained CPU can be throttled. This small host is intended for one runtime, not concurrent compilation, ETL and monitoring servers.

A Binance API IP whitelist needs a stable **outbound public IP**, not a private subnet. Whitelist the `WhitelistIPv4` output. Keep the same stack/EIP when updating application code. Replacement/deletion can change that address. SSM uses outbound TLS; public IP does not require inbound SSH. SSM access still requires appropriate permissions on the operator identity.

## Cost gate

Before deployment, query current Tokyo Linux/shared/on-demand t3.micro pricing and include all of:

```
730 * instance_hourly_USD
+ 730 * public_IPv4_hourly_USD
+ 8 * Tokyo_gp3_GiB_month_USD
+ outbound_data + S3_storage_and_requests + optional_logs + taxes
```

Target < USD 20/month. Prices have **not yet been verified** because the cloud environment blocks the AWS pricing endpoint. Do not treat the estimate or a budget notification as a hard spending cap. t3.micro has 1 GiB, below the suggested 2–4 GiB ECS memory range; this is an EC2 budget tradeoff. Changing to t3.small/ECS requires recalculation. An idle/stopped instance still incurs EIP and EBS costs; delete the disposable stack when finished. Export journals first: deleting the instance destroys its root disk. No snapshot retention or S3 bucket is created by this template.

Same-region S3 uses the free gateway endpoint and avoids NAT processing. S3 storage, requests and applicable transfer still cost money. Cross-region uploads, replication, Internet egress and unrelated AWS account costs are not eliminated. No broad S3 access is granted to the instance role; add a policy scoped to the chosen data bucket/prefix when one exists. Keep data and bucket in Tokyo. The endpoint routes traffic but does not grant S3 access.

## Validate and deploy

Use environment AWS credentials or a locally configured profile. In this cloud container the local `tokyo-stack` profile is absent, so unset `AWS_PROFILE` and `AWS_DEFAULT_PROFILE` for these commands. Never print or place AWS/Binance keys in the template, user data, Git or command arguments.

```sh
cfn-lint -r ap-northeast-1 -t ops/aws/testnet-host.json
aws cloudformation validate-template --region ap-northeast-1 \
  --template-body file://ops/aws/testnet-host.json
# Only after checking the cost gate above:
aws cloudformation deploy --region ap-northeast-1 \
  --stack-name mininautilus-testnet \
  --template-file ops/aws/testnet-host.json --capabilities CAPABILITY_IAM
aws cloudformation describe-stacks --region ap-northeast-1 \
  --stack-name mininautilus-testnet --query 'Stacks[0].Outputs'
```

Wait for SSM registration and verify the instance ID matches the stack before sending commands. First run `ops/probe_testnet.py` from this instance, then a paper session. Install/build the application for AL2023 on the host (or a matching build environment); do not assume a binary built against a newer container glibc runs on AL2023. Keep metrics bound to localhost; do not expose them publicly. Configure a bounded journal retention/export policy before unattended operation. Inject Testnet credentials securely only when enabling authenticated execution; this stack intentionally starts no order submission.

For a short reachability test without a persistent stack:

```sh
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE .venv/bin/python ops/ec2_probe.py \
  --region ap-northeast-1 --run-dir runs/tokyo-probe-new
```

That helper uses a default public subnet, creates a temporary instance/SG, collects public-only results and terminates/deletes them. It does not need an Elastic IP, IAM role or exchange keys. It downloads the pinned websockets 15.0.1 pure-Python wheel over verified TLS and checks its recorded PyPI SHA-256; no package-manager installation is needed. The guest stops after at most eight minutes; the controller terminates it and deletes its security group. If the controller is killed or loses AWS access, a stopped root EBS volume can remain billable: inspect the run’s resources.json and finish cleanup. IMDSv2 stays enabled so cloud-init can retrieve user data.

Delete the disposable persistent stack after exporting required data:

```sh
aws cloudformation delete-stack --region ap-northeast-1 --stack-name mininautilus-testnet
aws cloudformation wait stack-delete-complete --region ap-northeast-1 --stack-name mininautilus-testnet
```

Required cloud environment domains (in addition to Binance/package hosts): `sts.ap-northeast-1.amazonaws.com`, `ec2.ap-northeast-1.amazonaws.com`, `ssm.ap-northeast-1.amazonaws.com`, `cloudformation.ap-northeast-1.amazonaws.com`, `api.pricing.us-east-1.amazonaws.com`; regional S3 uploads use `s3.ap-northeast-1.amazonaws.com` with path-style addressing. Saved network drafts require Save/Publish before access is available.
