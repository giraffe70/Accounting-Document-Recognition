# OCR 效能與背景匯入

## 目前預設匯入流程

`POST /api/import-jobs` 的 `mode` 預設為 `manual_review`；明確傳入 `automatic` 則沿用先做方向及版面分析、再確認外觀的流程。手動覆核模式先執行零模型的頁面準備，頁面準備完成後即保存為 `prepared`，工作進入 `awaiting_review`。背景 worker 不會等待人工操作，因此其他工作可繼續處理。

多頁文件先批次準備頁面，再由使用者一次確認方向；單頁也使用相同確認流程。文件可由系統判斷 `auto_split` 並排入分割處理，也可由使用者手動分割，手動分割本身不呼叫模型。方向確認綁定頁面的來源 pixel hash；若影像被替換或修改，原確認失效，頁面回到 `prepared`。系統自動判定的單據類型維持自動來源，不標記為人工選擇。

人工確認後才建立可供版面分析與擷取共用的 `text_evidence`。可靠 PDF 文字層須至少有 20 個可列印字元、異常字元比例不超過 1%、可取得座標的字元至少占 95%，且 PDF 影像物件面積不超過頁面 10%；未達條件就退回 OCR。快速局部補讀最多使用一次 ROI OCR 和一次模型呼叫，30 秒是軟性時間預算，不會強制終止 Paddle。找不到安全 ROI 時保留首讀結果並標為待覆核。OCR 預設仍使用 `fixed_2400`，保留 text-line orientation；模型設定、解析度政策及後續辨識分屬不同階段。

資料庫新增 `ImportPage.preparation_json` 欄位，部署後需重啟服務，讓資料庫結構與新程式一致。舊工作快照沒有 `mode` 時按 `automatic` 解讀。原有執行版本檢查仍生效：程式、模型政策或來源檔案版本不符時，續跑或重試會回 `409`，需建立新工作。

目前新的 12 頁試點尚未完成，方向答案仍待人工覆核；不能據此宣稱已達 25% 效能改善門檻。舊試點數據只作歷史紀錄，不能代替新流程驗收。

## 背景工作與續跑

單檔、多檔及掃描來源現在使用同一個持久化工作服務。上傳只保存檔案及登記工作，回傳 `202` 後由單一背景執行緒逐頁渲染、辨識及保存。Paddle 推論使用程序內互斥；部署維持單一 Uvicorn worker。每頁的來源紀錄、子單據及完成狀態在同一資料庫交易提交，成功頁在續跑時直接略過。

| API | 用途 |
| --- | --- |
| `POST /api/import-jobs` | multipart `files`、`company`；回傳 `job_id` |
| `GET /api/import-jobs` | 目前登入者的工作清單 |
| `GET /api/import-jobs/{id}` | 頁級狀態、階段、預覽 URL、來源頁／單據 ID 與錯誤 |
| `POST /api/import-jobs/{id}/cancel` | 目前頁提交後停止 |
| `POST /api/import-jobs/{id}/resume` | 續跑取消或中斷工作 |
| `POST /api/import-jobs/{id}/retry-failed` | 保留成功頁，只重試失敗部分 |
| `GET /api/import-jobs/{id}/pages/{page_id}/preview` | 需登入且限工作建立者的頁面預覽 |

新工作只允許建立者查看或操作，既有單據沿用原權限。重啟後未完成工作顯示 `interrupted`，需由使用者按「續跑」。程式、模型政策或來源檔案變動時不沿用舊結果；執行版本不符的續跑回傳 `409`，需建立新工作。單頁失敗不阻止後續頁處理。舊 `/api/analyze`、`/api/batch`、`/api/job` 仍保留相容回應。

前端每 1.5 秒查詢各個工作，逐頁更新外觀確認清單；目前編輯內容及仍存在項目的勾選不會因更新而消失。整理或重新登入後從後端恢復工作。服務完整重啟會清除既有登入 session，需再次登入。

## 快取與計時

快取的原子寫入使用同目錄短隨機暫存檔，保留目的檔名、flush、fsync、Windows 重試與原子替換。寫入失敗仍保留本次記憶體結果並記錄原因。一般匯入同頁的版面、擷取與續跑共用穩定來源 token；驗證仍按組合、repeat、attempt 隔離。影像像素、實際 OCR 引擎、方向模型／政策及解析度政策納入相應快取識別。

計時版本 3 新增方向分類、解析度評估及首次可用結果時間。`ocr_seconds` 沿用實際 OCR 呼叫的定義，初始化是其子項；不要重複加總。畫面與匯出相容版本 2，沒有記錄的新欄位顯示未記錄。背景工作的 `first_result_seconds` 從登記工作開始計時，包含排隊；驗證頁面耗時不含排隊，兩者不可直接混比。

## 可選設定

在本機 `.env` 設定下列值後，完整重新啟動服務才會生效：

```dotenv
OCR_ORIENTATION_MODE=legacy
OCR_RESOLUTION_POLICY=fixed_2400
# OCR_DOC_ORIENTATION_MODEL_DIR=app/data/models/doc_ori
```

`OCR_ORIENTATION_MODE` 可設為 `legacy` 或 `auto`。`legacy` 沿用既有方向判斷；`auto` 才會呼叫文件方向分類器。預設模型目錄是 `app/data/models/doc_ori`，也可用 `OCR_DOC_ORIENTATION_MODEL_DIR` 指定其他本機目錄。只有指定目錄內存在 `*.pdiparams` 時才會載入分類器；模型缺少、載入失敗或信心分數低於 0.90 時，辨識會安全回到既有方向流程。服務不會為了方向分類器在辨識工作中下載模型。

`OCR_RESOLUTION_POLICY` 支援四種設定：

| 值 | 行為 |
| --- | --- |
| `fixed_1600` | OCR 長邊固定為 1600 像素 |
| `fixed_2000` | OCR 長邊固定為 2000 像素 |
| `fixed_2400` | OCR 長邊固定為 2400 像素，也是目前預設 |
| `adaptive` | 影像具備足夠清晰、稀疏文字證據時先用 2000；否則用 2400。若 2000 的結果品質偏低，最多再以 2400 重讀一次 |

解析度調整只影響 OCR 輸入副本；原始影像保留作為顯示與座標依據。Paddle CPU 的 oneDNN 維持關閉（`FLAGS_use_mkldnn=0`、`enable_mkldnn=False`）。

## 手動安裝方向模型

只有在明確要啟用 `auto` 時才需要模型。官方資產為 PaddleX `PP-LCNet_x1_0_doc_ori` 推論封裝：[官方模型封裝](https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-LCNet_x1_0_doc_ori_infer.tar)。本機 PaddleX 3.x 套件也將此名稱列為官方模型；封裝 URL 使用 `official_inference_model/paddle3.0.0` 路徑。

腳本只在人工執行時下載，預設輸出到 `app/data/models/doc_ori`；若該目錄已存在，會拒絕覆寫。它只接受一般檔案與目錄，拒絕絕對路徑、路徑穿越、連結及特殊檔案，並限制封裝與解壓大小。完成後會在 `manifest.json` 記錄來源 URL、下載時間與每個解壓檔案的 SHA-256。模型資產及 manifest 是本機執行期資料，不應提交到 Git。

先檢視說明，再明確執行下載：

```powershell
& "$env:USERPROFILE\Miniconda3\envs\pdfscan312\python.exe" -B -X utf8 scripts/prepare_ocr_orientation.py --help
& "$env:USERPROFILE\Miniconda3\envs\pdfscan312\python.exe" -B -X utf8 scripts/prepare_ocr_orientation.py
```

要安裝到別處時，可傳入一個尚不存在的 `--output-dir`。安裝完成後，在 `.env` 將 `OCR_ORIENTATION_MODE` 設為 `auto`；若模型在預設位置之外，同時設定 `OCR_DOC_ORIENTATION_MODEL_DIR`。

## 部署與驗收

程式碼、環境設定或模型政策改變後，完整停止並重新啟動服務。接著確認 `/api/validation/runtime` 顯示服務就緒，再建立新的驗證工作。執行版本或政策版本不一致時，舊工作續跑或重試會回 `409`；舊工作仍可查看或匯出歷史結果，不能拿舊工作續跑結果驗收新設定。

啟用 `auto` 或 `adaptive` 前，先以合成測試確認快取、方向 fallback、解析度升級和座標行為，再用至少 12 頁的代表性資料比較欄位準確率與端到端時間並人工覆核。未通過 12 頁門檻時，維持預設 `legacy` / `fixed_2400`。不要把私人掃描檔、資料集、gold 或模型檔加入 Git。

試點工具為 `scripts/benchmark_ocr_pipeline.py`、`scripts/benchmark_ocr_orientation.py` 與 `scripts/compare_ocr_pilot.py`，以各自 `--help` 查看參數。兩組使用同一凍結 manifest，以來源、頁碼及影像 SHA-256 精確對齊；比較前後核對答案雜湊，不修改 gold。驗證 API 的批次設定也可傳入非空、無重複的 `page_ids` 子集。方向衍生案例的舊流程角度是比較參照，不能取代獨立人工方向答案；出現差異時保留待覆核狀態，不自動當成模型錯誤或修改答案。

離線回歸執行 `scripts\verify.bat -Scope all`，再執行 `node --check app/static/js/app.js` 與 `node --check app/static/js/validation.js`。測試只使用合成資料及模型替身；實際速度與欄位品質另由固定頁面試點驗收。

## 歷史紀錄：2026-10-25 固定頁面試點

當日兩組以相同 12 頁、相同模型與提示詞實際執行；候選組使用 `auto` / `adaptive`。這是舊流程的歷史試點，僅代表當時 12 頁的結果，不是目前 `manual_review` 流程的驗收，也不能套用為所有文件或目前預設的保證效能。

| 指標 | 舊流程 | 候選流程 |
| --- | ---: | ---: |
| 每頁總耗時中位數 | 134.14 秒 | 86.39 秒 |
| 每頁總耗時 P95（線性插值） | 158.68 秒 | 117.53 秒 |
| 每頁平均 OCR 呼叫時間（含初始化子項） | 71.94 秒 | 37.22 秒 |
| 頁級完成／需人工處理 | 10／2 | 10／2 |

當時候選組中位數降低 35.59%，P95 沒有增加；但整體品質門檻未通過。一個單據號碼與一個單據類型出現舊組正確、新組錯誤；另有方向衍生案例與舊方向參照不同，需要獨立覆核。這些差異未被寫回 gold，也不能單憑與舊角度不一致就判定方向分類錯誤。歷史試點不表示目前新試點已達 25% 改善目標。

後續部署了快取修復與背景逐頁匯入；方向及解析度的設定仍依各階段政策處理，OCR 解析度預設為 `fixed_2400`。試點原始輸出、凍結 manifest、版本快照及比較報告保存在本機忽略追蹤的 `artifacts/ocr-performance-20261025/`；其中可能包含私人辨識內容，不應提交到 Git。
