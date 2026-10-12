# Lifecycle 本輪成果檢收

Created at：2026-10-12T20:05:09+08:00（Asia/Hong_Kong）  
審閱版本：`5c5fe90`；已 explicit fetch 遠端同名 branch，HEAD 一致。  
性質：code / evidence review；本次沒有重新部署、交易或重跑 EC2 soak。

## 結論

**接受為部分工程交付；不接受為 lifecycle 整體驗收完成。**
24 項：PASS 2、FAIL 4、INCONCLUSIVE 11、BLOCKED 4、NOT_MEASURED 3。
驗收 ledger 與報告一致，未發現把未跑的長測宣稱成功。

本次重新校驗 validation、disk、W1 final report 引用的 **384 個本機 artifact hash**（4+20+360）；全部一致。
另外從原始 W1 artifacts 重跑 `ops/lifecycle_report.py`，確認首次讀取增量下界 48,950,616 bytes，超過 1,048,576 bytes 預算。
這是本機 evidence 完整性核對，不是本次重新從 S3 下載全部備份，亦不代表全面重跑 correctness tests。

## 可以接收的成果

- Cold merge 改為 streaming；保留原格式與完整 checksum。H=1m 的 Rust HWM 最大值由 105,044 KiB 降至 2,544 KiB。這是單組件觀察，不能代替整個 actor process group 的記憶體驗收。
- Closed → Genesis 重試 anchor 修正、讀者 pin、opt-in GC、disk guard、sidecar fail-closed 和 admission timing 有實作及測試證據。
- 既有 validation 記錄為 VERIFY OK：78 Rust、138 Python、4 Node、20 parity seeds；39 次 publication crash 注入、100/100 timeout stress。此次只核對既存證據，沒有宣稱新的 test run。
- Batch reconciliation 的離線 oracle 保持結果一致；300→2 是指定 data requests 數目，不是 live latency 改善證明。

## 仍不接收的項目，按下一步影響排序

| Finding | 證據／原因 | 下一步 |
|---|---|---|
| 每次重寫全歷史 | 20 次輪換寫 982,058,640 bytes；預算 35,514,432 | 增量 immutable storage／有界維護；把 compaction 算進驗收 |
| Pin 導致磁碟無界 | 20 個 pin 累積 1,031,102,464 bytes；預算 213,561,344 | durable retirement catalog、retry、reader admission/backpressure；禁止刪仍被引用資料 |
| 首次查詢 O(history) | `ColdIndex::open` 逐 byte 驗完整 checksum；約113–119 ms | 明確決定完整性時機；不可只移走計時起點或略過 checksum |
| 輪換延遲超標 | H=1m median 325.54 ms，250 ms 是每次上限 | 改資料結構後重測完整 durable publication |
| 證據未足夠採用 | 短測 coverage、baseline receipt、固定 tail、完整 resume 尚有缺口 | 先補量度身分／固定 workload，再比較 |

重要限制：retirement catalog 單獨不能解決無限期 pin。只要每個 reader 保留不同完整副本，任何 GC 都不能同時「不刪活躍引用」及「磁碟永久有界」。必須限制新 reader／新 generation 的 admission，或讓版本共享 immutable pages；遭拒與停止輪換也必須記錄，不能冒充完成20次。

信任 ADR 仍未批准。頁級驗證即使使用 hash tree，也只證明讀到的頁，不能宣稱已檢查所有未讀資料；它與目前全檔首次驗證有不同漏檢窗口。

## 下一階段建議

採用 [LC-v2 fast iteration proposal](runtime-v2-lifecycle-fast-iteration-proposal.md)：先證據工具，繼而增量 cold storage／安全回收，再做固定 history 短測，最後每次15分鐘整合測試。
用15分鐘提早找錯，保留舊90分鐘 growth / availability 驗收為未完成；不要將15分鐘斜率外推成每小時 leak 結論。
現有量度主機是 EC2；未見本輪 ECS deployment evidence。下一階段預設重用 EC2；已有 ECS 時可用相同 profile，但不同環境不得混作 A/B。
