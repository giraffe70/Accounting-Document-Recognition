# 會計分錄與日記帳功能退役

AI 會計分錄建議、分錄草稿與確認、日記帳及會計傳票格式已退役。除摘要彙總表 API 外，相關會計模板端點回傳 404。辨識內容的草稿、人工確認與辨識報告列印仍保留；辨識確認不再表示或觸發入帳流程。

## 保留的辨識資料

紙面 `journal_voucher` 的借方／貸方 `entries`、紙面合計及來源 evidence 仍是辨識結果，舊 JSON 不會因功能退役而清洗。一般憑證的明細科目仍可編輯，品名科目記憶規則仍可保存與使用；此規則不是 AI 分錄建議功能。

## 部署與資料庫遷移

1. 停止服務，確認 SQLite 已無開啟中的寫入連線，再備份實際 `config.DB_FILE` 指向的資料庫。
2. 套用遷移，以 SQLite 交易重建 `voucher_records`，只移除 `entries_status`、`entries_confirmed_at`、`entries_confirmed_by`、`entries_confirmed_by_name`、`proposal_json`、`entries_history` 六欄。遷移可重複執行；辨識確認狀態維持原值，不改寫既有 JSON。
3. 驗證遷移前後資料筆數相同，並執行 SQLite 外鍵檢查；通過後重新啟動服務。
4. 重新登入並以 `Ctrl+F5` 強制載入前端，確認摘要彙總與辨識報告仍可用，退役模板端點回傳 404。

驗證批次的 `hash_version` 升至 4。舊 trace 保持唯讀可檢視，但不可續跑或混入新版結果。
