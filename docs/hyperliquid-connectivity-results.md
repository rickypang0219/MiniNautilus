# Hyperliquid Testnet connectivity handoff

Created: **2026-10-13T11:19:58+08:00**. Scope: read-only REST/WebSocket connectivity; migration implementation paused at the user's request.

## Result

**Tokyo EC2 PASS for REST, market streams and account subscriptions. Current cloud container BLOCKED by active egress policy.** No orders/cancels/transfers were issued. The private signing key was not sent to EC2.

Existing machine: `i-015087878f29f14cd`, `ap-northeast-1`. No new infrastructure, daemon, service or persistent trading process was created. Probe sockets close on completion. SSM invocation `5cf85030-fbb8-4a65-b09b-4b635ab1ecdf` completed successfully.

| Endpoint/subscription | Observed |
|---|---|
| HTTPS `https://api.hyperliquid-testnet.xyz/info`: meta | HTTP200, 212 instruments |
| REST l2Book / candleSnapshot | HTTP200, valid BTC book and 4 recent 1m candles |
| REST clearinghouseState / openOrders | HTTP200, account schema decoded; zero positions and zero open orders at probe time |
| REST extraAgents | Configured API agent address appears in the Testnet account's agent list |
| WSS `wss://api.hyperliquid-testnet.xyz/ws`: l2Book | Subscription confirmed, 6 book messages received |
| WS candle BTC 1m | Subscription confirmed, 1 candle update received; update receipt does not itself prove candle finality |
| WS userFills | Subscription confirmed, initial snapshot received, zero fills |
| WS userFundings | Subscription confirmed, initial snapshot received, zero funding records |
| WS orderUpdates | Subscription confirmed; actual order-event delivery NOT_MEASURED because no order activity was generated |
| WS ping/pong | PASS |

Account subscriptions are public address-based reads. They do **not** authenticate a signed exchange action. Key parsing and agent-list membership are prerequisites, not evidence that order signing/execution succeeds. No full migration, reconciliation/recovery, fill delivery or strategy-profitability acceptance is claimed here.

## Environment bindings

The user's actual injected names are `HL_NETWORK`, `HL_MAIN_ADDRESS`, `HL_API_ADDRESS`, `HL_API_PRIVATE_KEY`. All are present. Network value is Testnet. Address formats are valid. Local private-key parsing succeeds and the derived signer matches `HL_API_ADDRESS`. Values are omitted from artifacts.

Earlier migration prototype expects `HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS` and `HYPERLIQUID_TESTNET_PRIVATE_KEY`; these are **different names**. The next model should support the user's existing `HL_*` names explicitly and bind account queries to the main address, not the agent address. Do not ask for another copy of already injected secrets.

## Cloud access still needs activation

Both current-container HTTPS and WSS attempts were rejected by the proxy (CONNECT403 / InvalidProxyStatus). Read-only environment configuration inspection showed no Hyperliquid host in the active custom allowlist. A draft was saved adding **only `api.hyperliquid-testnet.xyz`**, preserving all existing custom destinations and presets.

Draft ID: `2d25c19b-cbe4-4cd6-9109-e3ea8a9f3005~cecfgdraft_6acc96bf9bbc8194afa95c854ceb4fed`. Tool result: `status=saved`, `requires_publish=true`. Saving a draft does not apply the runtime policy. Review/Save/Publish the environment and retry from the cloud before claiming current-container connectivity. No secret declarations, scripts or repository membership were changed in this step.

## Probe correction retained

The first probe compared acknowledgement dictionaries verbatim and incorrectly marked several subscriptions unconfirmed. Actual venue responses normalize user-address case and add defaults such as `nSigFigs=null`, `mantissa=null`, `fast=false`, `aggregateByTime=false`. The second probe validates the requested identity and these defaults semantically. Its result above supersedes the first verdict; the failed first result and redacted observed formats are retained.

## Evidence and next model

Raw summaries and SSM receipts: `runs/hyperliquid-migration/`. File hashes: `connectivity-evidence-index.json`. No user fill payloads, private key or signature was written into those artifacts; only counts/statuses and request receipts are retained.

Migration proposal: [hyperliquid-migration-proposal.md](hyperliquid-migration-proposal.md). Research proposal: [pca-stat-arb-research-to-paper-proposal.md](pca-stat-arb-research-to-paper-proposal.md). Adapter/runner/tests from the preceding implementation are still **uncommitted work in progress**. Preserve them and inspect `git status` before editing. This connectivity turn did not resume source implementation or publish code.

Next rigorous work: review actual Hyperliquid wire/history/margin contracts; fix environment-name compatibility; validate signing and order status against Testnet fixtures; exercise long/short/partial-fill/lost-ack/crash recovery under account limits; then execute the frozen soak and basket/portfolio acceptance gates. Connectivity success alone is insufficient to call the system reliable or PCA-ready.
