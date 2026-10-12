# Immutable cold pages v2

Created: 2026-10-12T20:31:03+08:00. Status: implementation candidate; owner approved root/path verification for isolated Testnet in this conversation (2026-10-12, Asia/Hong_Kong). Approval wording: “做先，如果差異大性能下降的話我們之後再switch back”. The preceding question stated the full unvisited-page detection window and current-segment startup scope. Performance adoption remains subject to evidence.

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


Reader/GC locking uses two files. `.cold-retirement.lock` serializes catalogue
mutations; each paged Index holds a shared `.cold-gc.lock` lease for its entire
lifetime, including historical readers that were never in the catalogue. Page
unlink requires an exclusive lease, so any reader conservatively defers all
retired-page disposal. Root pins remain per-file. The engine drops its own cached
Index before retrying collection. This prevents an old unregistered reader from
losing shared pages after a later registered generation is reclaimed.

A checksummed parent reference permits restart to repair one missed catalogue
registration after durable successor publication. A larger unknown lineage gap
fails closed; directory names are never used to infer ownership. Interrupted
migration intermediates and unregistered orphan pages are preserved for operator
inspection and remain included in disk accounting.

Known unavailable cold evidence creates a synced integrity-stop marker. Restart,
reconciliation and new-risk admission require a complete successful verification
before that marker can be removed. The approved unvisited-page detection window
does not allow a known corruption to be forgotten by restarting or reconciling.
`verify-cold` provides explicit full scrub; there is no periodic scrub scheduler.

`export-cold JOURNAL OUTPUT.cold` produces a fully verified MNCOLD01 export,
without changing the authoritative journal reference. It is an inspection/rebuild
primitive, not an automatic old-binary rollback: rebinding a live journal to an
exported root is not implemented. Stop executors and preserve uncertainty before
any operator migration or rollback. Live legacy-to-v2 adoption is not automatic.
