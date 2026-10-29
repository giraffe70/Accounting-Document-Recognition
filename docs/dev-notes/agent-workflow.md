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

指定歸屬公司後，匯入與審核頁顯示登錄統編；辨識及儲存時以 `company_info` 保存公司資訊，僅在交易方可配對或該類型的公司角色全空時補入統編，不改寫單據名稱、既有統編或人工清空值。自動值標記為 `field_evidence` 的 `source=company`，畫面與報告顯示「公司帶入」，不當成原圖辨識證據；更換／清除公司或變更類型會移除舊公司預設值。資料庫缺少統編時依穩定的科目表識別碼讀取 `companies.json` 補值，重新啟動時也會補齊資料庫，保留已有統編、公司 ID、名稱及科目表識別碼。離線回歸見 `tests/test_company_defaults.py`。

## 多單據結果與 offline 驗收

驗證輸出格式、辨識版本或單據切分契約有變時，建立新版本的驗證批次再檢查；已保存的舊批次不會補上 `documents[]`、`origin` 或 `quality`。可用合成輸入與模擬模型驗證 API、JSON/CSV 匯出和畫面，不把私人掃描檔、資料集或 gold 放進測試。`pages.csv` 每個輸入頁面只列一列；`vouchers.csv` 可依頁面所含文件展開多列，頁面耗時以 `pages.csv` 為準，避免按單據數重複加總。

OCR 計時與快取驗收範圍：快取以每頁結果獨立保存，不跨模型組合、重複次數或執行嘗試共用；一般上傳沿用 `source_token`，原圖內容改變或 OCR 引擎簽章不同時會失效。離線契約測試只驗證欄位與呈現，不代表實測節省時間。部署含計時或快取的新版本時，完整重新啟動服務並建立新批次驗收；頁面總耗時不含排隊，`ocr_init_seconds` 是實際 OCR 時間的子集，不應再重複加總。

補讀先按類型、已印標籤、格式及明確衝突決定目標；整頁低對比或傾斜只留下品質提示。發票與收據的核心缺值及有完整表頭／資料列的明細、分錄採保守白名單，其他可空欄位不因空值重讀。補讀區域先映射已有 OCR；新裁切只有在目標文字不清且可獲得原生細節或局部改善時才增加 OCR，增強副本不得取代原圖獨立證據。trace 的 `supplement_decision`、`supplement_region`、`supplement_ocr_decision`、`supplement_result` 可核對跳過原因、座標來源、新 OCR 請求與未採用原因。新策略需用新批次與人工覆核基準比較準確率和端到端耗時。

前端離線檢查可執行 `node --check app/static/js/validation.js` 與 `python -m unittest tests.test_frontend_form`；驗證 API／匯出契約使用 `scripts\verify.bat -Scope validation`。這些檢查不啟動真實模型，也不替代獨立人工證據核對。

辨識畫面中的 items 明細、科目記憶規則及辨識報告列印仍保留。紙面傳票的 `entries`、合計與 evidence 仍是辨識資料。AI 分錄建議、分錄草稿／確認、日記帳及會計傳票格式已退役；不要再依賴舊手冊中分錄狀態與入帳確認的說明。

## 會計分錄功能退役部署

部署時先停止服務，確認 SQLite 寫入連線已關閉並備份實際資料庫；以交易重建資料表，只移除 `entries_status`、`entries_confirmed_at`、`entries_confirmed_by`、`entries_confirmed_by_name`、`proposal_json`、`entries_history` 六欄。驗證資料筆數與外鍵後重啟服務，再以 `Ctrl+F5` 強制更新前端。遷移需可重複執行，辨識確認狀態保持原值，既有 JSON 不清洗。

獨立驗證批次 `hash_version` 升至 4。舊 trace 保留唯讀檢視，不可續跑；獨立驗證工具的 `classify_model` 仍代表單據分類模型，主設定僅保留 OCR 與 structure。

Ollama 輸出上限使用 `ChatOllama` 模型副本的 `num_predict`，由套件轉為 SDK 的 `options.num_predict`；不可使用 `bind(num_predict=...)`，它會把參數傳到 `Client.chat()` 頂層並造成 `TypeError`。`tests/test_retry_contracts.py` 與 `tests/test_recognition_optimization.py` 以實際安裝的 `ChatOllama`、保留 SDK 方法簽名的網路替身驗證參數、Schema 降級與設定保留，避免只有寬鬆模擬模型通過卻在正式套件失敗。

新結果以頁面 `result.id` 和單據 `parent_result_id` 關聯；單據 `id` 唯一識別單據，`document_index` 在資料中從 0 起算。`origin` 記錄來源頁與區域，`quality` 記錄內容類型、品質狀態及問題。舊結果沒有 `quality` 時應顯示未評估。品質狀態與 `completed_rate` 用於提示是否需要覆核及頁面擷取是否完成，均不代表欄位準確率；準確率只能由獨立覆核證據與標註評估。空白 `skipped` 頁不計入完成率分母，待分割 `partial` 頁仍需人工處理。來源頁只作追溯，不作為財務單據計入。

CSV 匯出新增 `pages.csv` 後，沿用含 `vouchers.csv`、`fields.csv`、`items.csv`、`entries.csv` 的相容格式進行評分；不要把 `pages.csv` 當作 vouchers 表格讀取。離線驗收不啟動 OCR/VLM/LLM，也不以結果完成率取代準確率檢查。

## 驗證服務版本、結果檢視與舊診斷

開啟 `/validation.html` 後，前端先讀取 `/api/validation/runtime`。只有 `ready=true`、`restart_required=false` 且 `schema_ready=true` 才能開始驗證、續跑或重跑失敗；端點不存在、載入版本與磁碟版本不一致或 schema 未就緒時，先完整重新啟動服務，再重新載入頁面。頁面不會自行重啟服務。執行新批次前確認 runtime 顯示就緒，並建立新批次以保存目前的程式、模型和契約版本。

本次更新後的操作順序：

1. 在目前服務的終端按 `Ctrl+C`，確認服務已停止；於專案根目錄重新執行 `start_webui.bat`（使用 `pdfscan312`）。
2. 登入後開啟 `http://127.0.0.1:8090/api/validation/runtime`，確認 `ready=true`、`schema_ready=true`、`restart_required=false`，且 `loaded_version` 與 `disk_version` 相同；再以 `Ctrl+F5` 重新載入 `/validation.html`。
3. 建立新批次，於「最新結果」檢視頁級狀態並匯出。舊批次僅供歷史與唯讀診斷；不要用它的重跑成績驗收新版本。

結果區預設「最新結果」：每個頁面、模型組合及重複次數只採用最新 attempt，避免把先前失敗與重跑結果累加。選「完整歷史」可查看每次嘗試；其筆數、成功率與耗時都代表執行紀錄，不能解讀成唯一頁面完成率。JSON、CSV 與 ZIP 匯出會沿用畫面選擇的 `view`；未帶 `view` 的舊 API 呼叫保留歷史匯出相容性。遇到舊批次混用程式或結果契約、或結果資料不完整時，最新完成率會顯示「無法可靠評估」。完成率只表示流程完成，不表示欄位正確。

舊結果可按需查看「保存的裁切結果／未驗收」診斷。它只讀取原頁、layout、已保存文件和 trace，供追查分割或執行問題；不回填正式 `documents[]`、不重新判定 quality、不納入統計或匯出，也不建立可入帳憑證。歷史批次不會因查看診斷而改寫；需驗收新契約時，修正服務版本後建立新批次。

## 帳號管理部署與驗收

部署前先備份現有資料庫。部署帳號管理更新後，完整重新啟動服務，啟動遷移會將舊 `role=admin` 轉為 `is_admin=true`，並將 `clerk`、`accountant`、`other:*` 轉為一般使用者，接著移除 `role` 欄位；使用者 ID、憑證、匯入紀錄及其他資料保留。遷移完成後舊版程式不相容，須使用新版重新啟動服務。啟動程序也會補上 `is_active` 欄位及 `username` 唯一索引；若資料有重複 username 或管理員權限資訊不一致，遷移會報錯並回滾，需先人工處理，系統不會自動更名或猜測權限。重新登入後按 `Ctrl+F5` 載入最新版管理頁面。離線測試僅使用暫存資料庫與合成帳號資料，不啟動真實模型。

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
