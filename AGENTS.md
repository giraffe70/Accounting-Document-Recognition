# 專案工作入口

- 正式程式位於 `app/`；先用 `rg` 搜尋相關符號，再定向讀取。`archive/` 為封存版本，只有歷史需求才讀取。
- 使用 `pdfscan312`（Python 3.12）與 UTF-8。驗證入口：`scripts\verify.bat -Scope contracts`；環境檢查用 `-Scope check`。
- 測試範圍與命令見 [驗證指南](docs/dev-notes/agent-workflow.md)。優先執行最小相關測試；核心或跨模組變更執行 `-Scope all`。離線測試不得呼叫真實模型。
- 單檔簡單查找由主代理處理；明確且可獨立的跨檔唯讀探索，委派 `economy-scout`。主代理已完成的探索不再重做。
- 指定檔案與驗收條件的低風險文件、測試、純函式或靜態 UI 小改，可委派 `economy-worker`。核心辨識、認證、SQL、資料遷移及會計邏輯由主代理處理。
- 委派時提供目標、檔案範圍、已知事實、驗收命令與停止條件；回報結論、符號/檔案證據、修改與驗證、未解問題。範例見 [委派協定](.codex/agents/delegation.md)。
- 不回退其他人的修改。Git 只提交任務相關程式與設定；不得提交私有掃描檔、資料集、gold、金鑰或執行期資料。
- 資料集/模型比較使用 `$model-validation`；類型/欄位/Router 變更使用 `$document-types`。不得把測試資料加入 few-shot 或為通過評分改 gold。
