# 憑證辨識系統（PDF_SCAN_Recognition）

票據憑證單據掃描辨識軟體 — 將掃描 PDF／圖片萃取為結構化會計資料，供會計師、記帳士審閱後用於稅務申報。

> 📖 開發線上手冊：`docs/manual/index.html`（用瀏覽器開啟，單一自包含檔）

## 快速開始（含 PaddleOCR，建議）

PaddleOCR 需 Python ≤3.12，故以 conda 3.12 環境執行整個應用（`start_webui.bat` 已指向此環境）：

```bash
conda create -y -n pdfscan312 python=3.12
conda run -n pdfscan312 python -m pip install -r requirements.txt -r requirements-ocr.txt
# 複製 .env.example 為 .env（雲端金鑰可留空，預設走地端）
# 啟動 Ollama 並 pull：qwen2.5vl:7b（辨識）、qwen2.5:7b（分類）、bge-m3（RAG）
start_webui.bat                                               # 開 http://127.0.0.1:8090
```

> PaddleOCR 帶來：OCR grounding（統編/金額更準）、精準邊界框、OCR 自動轉正。
> paddle 3.x 在 Windows CPU 需停用 oneDNN（程式已自動設 `FLAGS_use_mkldnn=0`）。

**不裝 PaddleOCR（純 VLM）**：改用 Python 3.13 `.venv` 裝 `requirements.txt`，系統自動降級為 VLM 直讀（框較粗、方向需手動）。

## 功能現況（Phase 1 已完成）

上傳 PDF/圖片 → 地端 VLM 辨識 → 憑證清單 → 審核區編輯（買賣方統編/金額/明細）→ 自動分類會計科目（借貸分錄 + 平衡檢查）→ 記帳士確認 → 會計師核定 → 單月日記帳彙整 → 列印/PDF。模型地端/雲端可於「模型設定」切換。

## 資料集驗證

登入後按右上角「🧪 資料集驗證」可開啟獨立驗證頁。選擇整個資料夾、圖片／PDF 或 ZIP，建立資料集後加入一至多組模型，即可依序執行：

- `VLM 直讀`：圖片直接交給 VLM，不使用 OCR。
- `OCR＋VLM`：OCR 文字與圖片共同交給 VLM；OCR 不可用時會明確標記降級路徑。
- `OCR＋純文字 LLM`：只把 OCR 文字交給 LLM；OCR 失敗時整張標記失敗，不會改走 VLM。

每張圖片或每頁 PDF 視為一張憑證。頁面可查閱原始頁圖、實際模型輸入圖、含框圖片、完整欄位／明細／分錄、OCR 結果、原始模型回應及逐階段計時。批次可取消後續跑，也可「終止批次」以停止尚未處理的項目；已完成結果保留，終止後不可續跑。另可重跑失敗項目，並匯出 JSON、CSV ZIP 或包含所有證據的完整 ZIP。

驗證資料獨立保存在 `app/data/validation/`，不會寫入正式憑證資料庫；此目錄已排除於版本控制。伺服器重啟後，未完成批次會標記為「已中斷」，可從頁面續跑。

「圖片長邊」是送入辨識管線的圖片寬、高較大者上限（預設 1180 像素，可設 640–2400）；大圖等比例縮小，小圖不放大。比較不同模型時應固定同一數值。「每組先暖機一次」會讓每個模型組合先額外辨識第一張憑證，保留該次紀錄，但不納入主要速度統計。資料集與批次會持久保存；「清空畫面（不刪資料）」只在目前瀏覽器隱藏既有紀錄，按「顯示歷史紀錄」即可恢復。清空畫面不會停止正在執行的批次。

計時欄位分為前處理、OCR／方向判定、單據分類、欄位萃取、證據輸出、資料持久化、辨識總時間、含存檔總時間與排隊時間。PDF 轉圖耗時平均分攤至各頁並另外列示，不重複計入辨識總時間。統計中的平均、P50、P95 只使用成功項目，失敗／逾時另計；暖機結果不納入主要統計。

預設限制為單檔 100 MB、資料集解壓總量 2 GB、5,000 個檔案，分別可用 `VALIDATION_MAX_FILE_MB`、`VALIDATION_MAX_TOTAL_MB`、`VALIDATION_MAX_FILES` 環境變數調整。單張辨識逾時可在建立批次時設定（30–3600 秒）。

## 資料庫檢視（管理員）

以管理員帳號登入後，點擊頂部「🗃️ 資料庫檢視」進入 `/database.html`。
先選擇「憑證與帳號」或「資料集驗證」資料庫，再選擇中文名稱的資料表；表格欄名與用途說明以中文顯示，原始欄名可從欄位提示查看。可依關鍵字搜尋並選擇每頁 25、50 或 100 筆；有日期或狀態的資料表會顯示對應篩選欄位。憑證紀錄以憑證日期篩選，驗證資料庫的日期依台灣時間計算。長文字與 JSON 可在右側面板查看。
此頁僅供唯讀查詢，密碼雜湊與 salt 由後端遮蔽且不納入搜尋。非管理員無法呼叫資料檢視 API。
新增功能後需重新啟動 WebUI 並重新整理網頁，才能載入新的 API 與入口。

## 目錄結構

- `app/` — 正式版程式碼（FastAPI + 混合 OCR/VLM，地端/雲端可切換）
- `archive/phase0_poc_flask/` — 第一期概念驗證 POC（Flask + Gemini，已封存凍結）
- [目前系統架構](docs/dev-notes/architecture.md) — FastAPI、SQLite、辨識與審核資料流
- [POC 歷史架構](docs/dev-notes/architecture-phase0.md) — 已封存的 Flask 概念驗證版本
- `docs/manual/index.html` — 開發線上手冊
- `ref/phase1/` — 第一期報價單
- `ref/phase2/` — 第二期委託技術服務合約草稿

## 技術方向（重新規劃）

- 後端：**FastAPI**（參考 `research_log_agent`），WebUI 為原生 HTML/JS。
- 辨識：**混合 OCR + VLM/LLM** — PaddleOCR（地端）取精確文字/座標 → VLM/LLM 結構化 → 會計科目分類 → 日記帳彙整 → 人工審核回饋。
- 模型：**地端/雲端可切換**（地端優先）。地端 PaddleOCR + Ollama（Qwen2.5-VL、Qwen2.5、bge-m3）；內網 H200 vLLM（Qwen3-32B，需 VPN，見 `C:\Dev\H200\docs\api-usage-prompt.md`）；雲端 Gemini/Claude/GPT。
- 完整規劃見 Phase 1／Phase 2 實作項目。

