# Agent 工作與驗證指南

## 環境與最小驗證

專案入口 `scripts/verify.bat` 呼叫 `scripts/verify.ps1`（ExecutionPolicy 僅設於該次 PowerShell 程序，不修改全域設定）， 預設使用 `%USERPROFILE%\Miniconda3\envs\pdfscan312\python.exe`、UTF-8 與 `-B`。需要其他環境時明示 `-Python <python.exe>`；不自動退回系統 Python。開發依賴見 `requirements-dev.txt`，缺依賴時入口報錯，不自動安裝。

| 修改範圍 | 命令（專案根目錄） |
| --- | --- |
| Agent/Skill 設定、環境 | `scripts\verify.bat -Scope check` |
| 單據契約、Router、純解析/正規化 | `scripts\verify.bat -Scope contracts` |
| 資料庫檢視 | `scripts\verify.bat -Scope database` |
| 驗證 API/排程 | `scripts\verify.bat -Scope validation` |
| gold 組合、保存結果評分 | `scripts\verify.bat -Scope scoring` |
| 核心或跨模組修改 | `scripts\verify.bat -Scope all` |

測試使用合成資料、暫存目錄及模擬模型，入口阻擋 TCP 連線。Windows asyncio 的內部 socketpair 通道仍可使用。無模型契約測試涵蓋類型欄位唯一性、手冊/報表映射、Router 回應解析、日期/金額與 JSON 修復；這些測試不代表實際辨識準確率。

## 保存結果評分

所有命令以 `pdfscan312` Python 的 `-B -X utf8` 執行。新輸出目錄必須不存在，不覆寫舊結果。

```powershell
conda run -n pdfscan312 python -B -X utf8 analysis/evaluate_validation.py --run qwen=<CSV資料夾A> --run gemma=<CSV資料夾B> --gold <凍結gold.json> --dataset-id <資料集ID> --output-dir artifacts/evaluation/<新評估名稱>
```

每個 CSV 僅含一組模型及一次重複執行，資料夾需包含 `vouchers.csv`、`fields.csv`、`items.csv`、`entries.csv`。`--run` 恰好兩組，名稱可自訂；歷史報告產生器仍預期 `qwen`/`gemma` 欄位。共同頁面以 `(source, page)` 對齊，沒有共同頁面或重複鍵則拒絕執行。gold 為含 `source`、`page`、覆核欄位與 `confidence` 的 JSON 陣列。

若需資料庫核對批次設定與 trace，明示 `--database <validation.db> --run-id qwen=<ID_A> --run-id gemma=<ID_B>`。資料庫以唯讀 URI 開啟；核對資料集、圖片上限、重複次數、暖機、提示詞/程式雜湊與模型參數。未提供兩組完整 metadata 或 CSV 辨識模式時，摘要標明 `comparability_verified=false`，需人工確認 CSV 實際來源及條件；`csv_run_identity_verified=false` 表示程式沒有把 CSV result ID 與 DB 逐一核對。

`--deduplicate` 需提供資料庫與全部共同頁面的原圖。每組相同 SHA-256 影像按 `(source, page)` 排序保留第一頁；gold 衝突則拒絕執行。摘要列原始/計分頁數、去重排除頁及影像覆蓋率，無影像證據時唯一影像數為 `null`。

輸出為 `validation_comparison.csv` 和 `validation_summary.json`；摘要保存來源識別、輸入 SHA-256、評分程式雜湊與時間。凍結 gold 與 CSV 只讀，結束前再核對雜湊。

全圖 evidence 評分亦需明示來源：

```powershell
conda run -n pdfscan312 python -B -X utf8 scripts/score_full_image_validation.py --evidence <recognition_evidence.json> --builtin-gold --dataset-id <資料集ID> --run-id <批次ID> --output-dir artifacts/evaluation/<新評估名稱>
```

`--builtin-gold` 明示採用腳本內原有 40 張覆核答案；其他樣本改用 `--gold <JSON>`，格式為 `{ "圖片檔名.jpg": { "欄位": "人工覆核值" } }`。來源檔名碰撞則拒絕執行。兩個評分命令都不啟動辨識。

## gold 組合與版本基準

評分不再組合 gold。只有明確要求整理已覆核標註時使用 `analysis/prepare_gold.py --base <檔案>`（可重複）`--override <檔案>`（可重複）`--output-dir <新目錄>`。base 重複鍵拒絕，override 按明示順序覆蓋；匯出 JSON 和輸入雜湊 manifest，不修改來源 gold，也不自動判定標註對錯。

Git 基準只包含程式、設定與開發說明；私有掃描、資料集、gold、資料庫和模型產出由 `.gitignore` 排除。使用 `git status --short`、`git diff -- <本次檔案>` 核對改動，按任務檔案清單提交，不使用 `git add .`。已建立優化前的原始程式 commit，可用 commit 比較追蹤變更。

在不同 Windows sandbox 身分遇到 dubious ownership 時，僅針對此已確認的本機專案使用 `git -c safe.directory="C:/Projects/Accounting Documents/pdf-scan-recognition" ...`；不設定全域萬用信任。

委派格式與回報見 [.codex/agents/delegation.md](../../.codex/agents/delegation.md)。設定數量保持精簡；成本需依實際任務總耗時、可取得 Token 用量及返工率觀察。
