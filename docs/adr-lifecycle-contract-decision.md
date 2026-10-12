# LC contract decision record

Created at: 2026-10-12T16:10:00+08:00. Status: approved for isolated Testnet current-segment startup and immutable-page root/path verification by explicit owner reply on 2026-10-12; see adr-cold-pages-v2.md. This approval does not waive acceptance gates or authorize mainnet.

The existing `b0223f3` rotation implementation validates only the current journal
at startup and the complete current cold index at first use. No explicit owner
approval of this departure from pre-Runtime-v2 full-history validation has been
found in this task's instructions. LC does not silently mark it approved.

The changes prepared here preserve `MNCOLD01` byte layout, full-file checksum
validation and journaled retired verdicts. Streaming merge is bounded in RAM;
it still reads/writes all retired history. Reclamation is a separate opt-in:
only the immediately superseded immutable cold file is considered, after the
successor genesis is durable and the successor index is verified. Readers pin
files with shared locks; pinned files are skipped. Journals are never deleted.
Historical read-only replay needs only journaled verdicts; an operator requesting
`verify-cold` on a reclaimed old generation will find it unavailable. Current
recovery and dedup use the newest complete cold evidence.

For live adoption the owner must choose whether to accept the existing current-
segment/first-use integrity contract. Its failure window is precise: corrupt old
journal bytes after rotation are not detected at startup; a corrupt cold index
can remain undetected until first use or explicit `verify-cold`. The checksum is
corruption detection, not authentication. Streaming and GC do not close that
window. Rejecting this contract means keeping live rotation disabled, preserving
full-history validation and reporting archive-independent startup BLOCKED.

Cold first-query bytes and repeated full-file writes cannot become archive-
independent within the unchanged format. A future authenticated paged index or
persistent tree needs its own concrete format, crash matrix and integrity
comparison. That change is not implemented or approved by this record.

Offline fault experiments remain permitted. Testnet rotation adoption stays
BLOCKED until the contract choice is explicitly recorded; ordinary compatible
code, offline measurements and safety testing continue.
