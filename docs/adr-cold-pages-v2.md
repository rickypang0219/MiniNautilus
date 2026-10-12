# Immutable cold pages v2

Created: 2026-10-12T20:25:00+08:00. Status: implementation candidate; owner approved root/path verification for isolated Testnet in this conversation (2026-10-12, Asia/Hong_Kong). Approval wording: “做先，如果差異大性能下降的話我們之後再switch back”. The preceding question stated the full unvisited-page detection window and current-segment startup scope. Performance adoption remains subject to evidence.

MNCOLD01 remains readable and the default writer. Candidate MNCOLD02 stores two
immutable B+ trees (order ID, execution ID). Each 4096-byte page has a type, level,
record count, sorted keys, zero padding and chained FNV corruption checksum.
References bind maximum key, record count and page checksum. Root manifest binds
both trees and total counts to the existing journal ColdRef checksum. FNV is
accidental corruption detection, not authentication.

Leaves hold up to 120 32-byte records, branches up to 120 child references.
Batch update partitions sorted new keys by existing child ranges; each affected
page/ancestor is rewritten once per batch, split pages have immutable names.
This bounds fanout to tree height instead of the number of historical segments.
At 1m records each index has about 8,334 leaves, 70 branches and a root, roughly
69 MiB for two trees, versus 49 MiB logical baseline. Append 200 keys per index
usually rewrites 2–3 leaves and 2 ancestors: ~40KiB/rotation. Twenty cycles plus
root metadata are well under the 35.5MB write budget in this estimate. Scattered
keys can touch many more leaves; counters and adversarial fixtures decide, not
this estimate. All new writes, retries, GC and migration are counted separately.

Default open verifies the complete reachable tree before any lookup, preserving
full-index first-use corruption detection. Explicit offline PathVerified open
checks the root and verifies each page on the selected path. Unvisited damage
is detected only by a later query or full scrub. PathVerified is approved for isolated Testnet by the owner reply recorded above; no mainnet adoption is authorized. Root checksum comes from durable journal metadata; no
cryptographic authenticity claim is made.

Publication: immutable page create/write -> file fsync -> page directory fsync
-> create root temp -> file fsync -> rename -> journal-directory fsync. Existing
identical immutable pages/root may be reused after verification and resync;
conflicting survivors fail closed. Closed/Genesis publication remains in the
journal layer. Failed publication may leave orphan pages; conservative collection
requires authoritative current roots and all reader pins, never name guessing.

A migration streams verified MNCOLD01 rows into bounded batches; original file
is preserved. Old binaries cannot read MNCOLD02; rollback therefore requires
verified export/rebuild or retaining the old deployment until no v2 writes have
occurred. Replacing a binary alone is not rollback.

GC must serialize reader registration with reachability collection, keep current
and pinned roots, and count shared pages/metadata in allocated space. A full
reachability sweep is initially a correctness implementation, not a claim of
constant-time maintenance. Production adoption requires measured bounded
maintenance, catalogue retry and crash recovery. The implementation records replaced page hashes in the checksummed successor root; a durable catalogue carries this debt across generations. Conservative GC retains replaced pages while any retired generation is pinned, then retries disposal. Preflight rejects growth beyond the numerical disk/debt budget. Full reachability sweeps are not used on every rotation. This ADR does not approve page
removal merely because a publication failed.
