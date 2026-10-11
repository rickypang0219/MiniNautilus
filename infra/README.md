# MiniNautilus AWS infrastructure (CDK, TypeScript)

One always-on Graviton EC2 host in **ap-northeast-1** for the engine, laid out like
`rickypang0219/trading-system` (`bin/` app, `lib/` stack, jest assertions), but
without that system's serverless pieces: MiniNautilus is a single stateful process.

```
VPC 10.42.0.0/24 (1 AZ)
└─ public subnet (no auto public IP) ── Internet Gateway ── Binance / SSM / packages
   ├─ EC2 t4g.micro, AL2023 arm64, gp3 12 GiB encrypted, 2 GiB swap, IMDSv2 only
   │   └─ Elastic IP  = fixed egress IP for the Binance API whitelist
   └─ S3 gateway endpoint (free) ── artifacts bucket (runs/ expire after 90 days)
No NAT Gateway · no inbound rules (SSM Session Manager) · no interface endpoints · no ALB
```

## Cost (Tokyo on-demand, Price List API, 2026-10-11)

| Item | Price | Month (730 h) |
|---|---:|---:|
| t4g.micro | $0.0108/h | $7.88 |
| Public IPv4 (the EIP; charged in use or idle) | $0.005/h | $3.65 |
| gp3 12 GiB | $0.096/GiB-month | $1.15 |
| **Fixed total** | | **$12.69** |

`-c instanceSize=small` (t4g.small, 2 GiB) is $20.2/month with the same disk.
`bin/mininautilus.ts` refuses to synthesize if the fixed cost exceeds `budgetUsd`.

Variable costs not in the fixed total: Internet egress (first 100 GB/month free
across the account; WebSocket candles and REST calls are far below that), S3
storage/requests (cents), tax. **Why no NAT:** a NAT Gateway costs about $45/month
in Tokyo before data processing (~$0.062/GB); a public subnet + EIP gives the same
fixed outbound address for whitelisting at $3.65. S3 traffic uses the gateway
endpoint, so journal uploads never touch the Internet path. CPU credits are
`standard`: sustained load is throttled, never billed as surplus credits.

An AWS Budget (`<stack>-monthly`, $20) is created; pass `-c alertEmail=you@x` to get
forecast alerts at 80% and 100%. A budget alerts, it does not cap spending.

## Commands

```sh
cd infra && npm ci
npm test                     # cost/security assertions (no NAT, one IPv4, IMDSv2, no ingress)
npx cdk synth
npx cdk deploy MiniNautilusHost [-c instanceSize=small] [-c alertEmail=...]
npx cdk destroy MiniNautilusHost   # bucket is retained; empty/delete it separately
```

Credentials come from the environment. Values must be plain text: an `AWS_REGION`
written with typographic quotes (`“ap-northeast-1”`) makes the CDK CLI fail with
"no credentials have been configured". In the cloud session use
`env -u AWS_PROFILE AWS_REGION=ap-northeast-1 npx cdk ...`.

## Deployed (2026-10-11)

Stack `MiniNautilusHost`: instance `i-015087878f29f14cd`, whitelist IP `54.92.99.11`.
Verified through SSM: bootstrap complete, 2 GiB swap, Python 3.11, egress IP equals
the EIP, Testnet REST ping/time/bookTicker HTTP 200 in ~27 ms, IMDSv1 refused (401),
IMDSv2 allowed. No exchange keys or application are installed yet.

## Next

1. Build the engine on the host (`dnf install -y rust cargo` or rustup; fat LTO uses the swap) or upload an aarch64 binary to the bucket.
2. Store Testnet keys in SSM Parameter Store (SecureString, standard tier is free) and grant the role `ssm:GetParameter` on that path only.
3. Run `ops/probe_testnet.py`, then `ops/soak.py` (paper, then testnet) on the host; copy `runs/` to the bucket.

`ops/aws/testnet-host.json` is the earlier hand-written x86 template; this CDK app supersedes it.
