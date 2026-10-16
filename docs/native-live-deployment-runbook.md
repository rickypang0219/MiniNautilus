# Native live deployment preparation

Created: 2026-10-16T19:29:00Z (UTC).
Mode: dedicated-account **Hyperliquid Testnet**. The selected first real strategy is Cluster-PCA **shadow, without orders**.

This runbook prepares a supervised rollout. It does not establish an unattended production service. Current implementation and failed attempts are recorded in [migration results](rust-native-runtime-results.md); the subsequent implementation/drill scope is [production readiness proposal](native-production-readiness-proposal.md).

## Release and evidence

Build on a verified Linux environment with the locked dependencies:

```sh
scripts/verify.sh
cargo build --locked --release --bin mininautilus
cargo build --locked --release --features native-runtime --bin native-live
python3 ops/native_bundle.py --output runs/native-release-UNIQUE.tar.gz
```

The bundle command requires a clean committed tree and records source/file hashes in `BUILD.json`. Archive its printed receipt with verify output, compiler version, workload manifest and acceptance report. It packages the two explicit executables, without credentials or `.env` files.

Before execution, verify every receipt hash, binary CPU architecture and dynamic-library requirements on the **actual** target. The measured c7i/Ubuntu x86 release is not an ARM/t4g/AL2023 release. Compile and test that target separately. Release profile and the binary hash both matter; moving a debug executable into a release directory does not qualify it.

## Host and state preparation

Use Tokyo, no inbound ports, SSM administration, IMDSv2, encrypted disks and the same-region S3 gateway. Preserve the user's existing t4g host. Temporary acceptance instances are task-owned and terminated after original journals are archived and account cleanup is independently checked.

For a persistent deployment, first implement the retained data-volume and host-replacement gates in proposal O. The existing `infra/` root EBS uses delete-on-termination. **Do not rely on it as the only live journal copy.** Store these together on the retained state volume:

- Core `journal.jsonl` and any successor/cold-page files;
- `actions.jsonl` containing durable action attempts/nonce history;
- `session.json`, instrument/config/account binding and current-session lineage;
- the persistent operator-stop marker once implemented.

S3 async backups supplement durable local state. They do not prove RPO0 for disk/AZ loss. Never restart from a stale backup and assume a flat venue account means missing actions did not exist. A missing action history is an unresolved incident.

Set `HL_NETWORK=testnet`, `HL_MAIN_ADDRESS`, `HL_API_ADDRESS`, `HL_API_PRIVATE_KEY` using secure runtime injection or SSM SecureString. Do not put values in Git, process arguments, user data, ordinary S3 artifacts, SSM command strings or service-unit files. The optional strategy child receives a cleared environment; use trusted strategy code, since same-user processes are not a security sandbox.

## Before any signed run

1. Verify no other host/container/local Python runner is trading this account. A local `flock` protects only the local host. Keep one active host until cross-host fencing has been drilled.
2. Check actual venue positions and open orders through `/info` (`clearinghouseState`, `openOrders`) using the dedicated account. Save a dated, account-aliased result without secrets. A process scan alone cannot establish flatness.
3. Locate all interrupted sessions. Recover each with its original journals before creating a new session. If outcomes remain uncertain, stop; do not change session IDs to pass the flat-genesis check.
4. Confirm BTC-only dedicated account, cross leverage1, minimum NAV100USDC, 20-lot target/0.0002BTC, maximum order40USD and session loss10USDC. These are diagnostic/Testnet bounds, not a PCA portfolio risk policy.
5. Public/private subscription probe must pass using the receipt-verified binary:

```sh
target/release/native-live probe --seconds 15
```

The probe does not place orders. Actual signed admission separately rechecks account/agent binding, mode, quote/account freshness and risk before signing.

## Supervised acceptance and stopping

Use a unique run directory and keep the terminal/controller alive:

```sh
target/release/native-live paper --python-strategy --seconds 900 --flatten --run-dir runs/native-paper-UNIQUE
target/release/native-live testnet --python-strategy --seconds 900 --flatten --run-dir runs/native-testnet-UNIQUE
```

The second command temporarily holds small long/short Testnet exposure. Record its PID and full run-directory path. Send SIGTERM to that **recorded PID** for an orderly stop; avoid broad process-name kills. SIGTERM closes admission and attempts cleanup. A request already handed to the venue can still fill; host loss/SIGKILL cannot guarantee flattening.

After an interrupted run, explicitly recover the original session:

```sh
target/release/native-live recover --flatten --run-dir runs/native-testnet-UNIQUE
```

Recovery resolves original identities; it does not resume the strategy. Require a successful recovery report and a fresh independent venue query showing zero positions/open orders. Keep the original failed run marked FAIL even if recovery succeeds. Wrong/missing/corrupt journals and unowned activity require investigation.

Current lifecycle is bounded: exact `1000/Expired` renews at most three times per invocation; general disconnects stop. At 1,000 orders the session refuses further strategy orders. Live Hyperliquid rotation is pending L2. Do not configure an indefinite restart loop around this command: a new process is not a substitute for reconciliation or rotation.

## Upgrade and rollback rehearsal

For the current supervised mode: stop the old run, drain and recover it, confirm old PID exited and venue state is resolved, archive original state, then verify the replacement build. Only then start a fresh flat-genesis session. If the account is not flat, retain original-session recovery ownership; do not launch a replacement strategy.

Rollback can select an older compatible binary only after those same checks. Never remove or rewrite journals to fit the old executable. A binary that cannot read a newer state/format must refuse startup. Same-account replacement on another host additionally requires proof the old host/signing capability is stopped. A lease alone cannot fence an old signer at the exchange.

Persistent service installation, automated upgrades, durable kill marker, alert delivery, retained-EBS host replacement and incompatible-schema rollback are **pending drills**, not delivered capabilities. Implement proposal O before unattended operation; do not represent the temporary experiment controller as a production deployment stack.

## Promotion record

For every attempt retain source SHA, binary/bundle/manifest hashes, host/region, start/end timestamps, exit code, session/order/fill IDs, journal chain, telemetry drops, process-tree resources, reconciliation/cleanup and EC2/EBS/secret deletion receipts. A successful SSM supervisor command alone is insufficient: inspect the nested trading exit code and duration. Experiment4's supervisor succeeded after recovering a failed signed run; the signed run remains FAIL.

Require ≥900 active seconds (±5s), combined RSS≤384MiB, terminal owned orders, no unexplained ledger discrepancy, final flat/open-orders0 and no unaccounted action. Check expiry trace generation/epoch and fresh subscription/reconciliation, not only process liveness. Order counts and strategy outcomes remain separate; a short SMA session does not qualify PCA alpha.

The next deployment milestone is therefore: corrected signed Testnet acceptance → L1/L2 + O drills → Cluster-PCA causal replay and shadow → portfolio paper/Testnet. General mainnet promotion stays outside this migration's scope.
