# 憑證辨識系統

將掃描 PDF／圖片轉為結構化憑證資料，提供原圖核對、欄位與明細編輯、會計分錄草稿、人工確認及報表輸出，供會計師、記帳士處理帳務。

本文件依目前 `app/` 正式程式整理（2026-10-28）。操作與架構細節見 [開發線上手冊](docs/manual/index.html)（用瀏覽器開啟）及 [目前系統架構](docs/dev-notes/architecture.md)。

## 快速開始（Windows／Python 3.12）

使用 conda 的 `pdfscan312` 環境。先安裝並啟動 Ollama，再於專案根目錄以 PowerShell 執行：

```powershell
conda create -y -n pdfscan312 python=3.12
conda activate pdfscan312
python -m pip install -r requirements.txt -r requirements-ocr.txt

# 首次安裝建立設定檔；保留既有 .env
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

ollama pull qwen2.5vl:7b
ollama pull qwen2.5:7b
.\start_webui.bat
```

開啟 [WebUI](http://127.0.0.1:8090)。程式內建的地端欄位萃取與會計分類模型分別為 `qwen2.5vl:7b`、`qwen2.5:7b`。`bge-m3` 用於分類案例的 RAG 檢索，需要時另執行 `ollama pull bge-m3`。

`start_webui.bat` 優先使用目前啟用的 conda 環境，否則使用 `%USERPROFILE%\Miniconda3\envs\pdfscan312\python.exe`，啟動位址固定為 `127.0.0.1:8090`。若要自訂位址或連接埠，可在啟用環境後直接執行：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```

PaddleOCR 提供文字、座標與方向判定；Windows CPU 的 oneDNN 相容設定 `FLAGS_use_mkldnn=0` 已由程式處理。若只需純 VLM，可在同一個 Python 3.12 環境只安裝 `requirements.txt`；一般辨識在 OCR 不可用時會降級，驗證頁則可明確選擇「VLM 直讀」。部署後可從「模型設定」確認 OCR 可用狀態。

## 首次登入與部署資料

首次啟動且使用者資料表為空時，系統建立下列初始帳號；已有帳號的資料庫會沿用原帳號。

| 帳號 | 初始密碼 | 權限 |
| --- | --- | --- |
| `admin` | `admin123` | 系統管理員 |
| `clerk` | `clerk123` | 一般使用者 |
| `accountant` | `acc123` | 一般使用者 |

首次部署請立即變更初始密碼。管理員可由「權限管理」新增一般使用者、編輯帳號、變更密碼或停用帳號；停用會保留歷史關聯。伺服器端維護工具見 [scripts/manage_users.py](scripts/manage_users.py)，例如：

```powershell
conda activate pdfscan312
python -B -X utf8 scripts/manage_users.py passwd admin
```

密碼以互動方式輸入。建立其他管理員可使用該工具的 `add --admin` 命令。

`app/data/` 為執行期資料，已排除於版本控制。全新下載程式碼後，應另行備妥部署所需的 `accounts.json`（科目表）、`companies.json`（公司資料）與相關設定；缺檔時公司／科目清單為空。搬移既有系統時，也需保存資料庫、原始影像及模型設定。

## 目前操作流程

1. **匯入與外觀確認**：選擇歸屬公司，上傳 PDF／圖片，檢視頁面並確認方向與分割範圍，再進行內容提取。自動版面分析可將一頁拆成多張單據，來源頁保留供追溯。
2. **辨識內容確認**：對照原圖，編輯單據類型、日期、買賣方資料、金額及明細，可先儲存草稿，再確認辨識內容。指定公司的登錄統編可能作為預設值帶入，畫面標示「公司帶入」供核對。
3. **明細科目與分錄草稿**：可編輯品名對應科目；勾選「記住對應」並儲存後，符合條件的規則依公司共用。一般單據可產生 AI 分錄建議，經預覽後選擇採用並替換草稿，也可手動新增、修改或刪除分錄。
4. **確認分錄供入帳**：辨識確認與分錄確認分開進行。分錄確認需指定公司、完成科目及金額、每列具有單一借貸方向，且借貸差額絕對值小於 0.01；確認後才會納入日記帳及正式傳票。
5. **報表輸出**：依所需期間檢視日記帳、辨識明細彙總及列印報表。辨識報告需先確認辨識內容；正式帳務報表則依分錄確認狀態篩選。

辨識內容可先確認，即使科目或分錄尚待核對。原始會計傳票保留紙面分錄；單面傳票可保存草稿，需人工補齊並核對後才能確認入帳。編輯來源欄位、公司或影像等內容後，相關確認狀態會撤銷，需重新覆核。

細節見 [分錄編輯與入帳確認](docs/dev-notes/entry-workflow.md)、[品名科目對應](docs/dev-notes/item-account-mapping.md) 及 [公司會計政策](docs/dev-notes/accounting-policy.md)。

## 模型與技術架構

- **後端與介面**：FastAPI、SQLite／SQLModel，WebUI 使用原生 HTML／JavaScript。
- **辨識**：PaddleOCR 取得文字與座標，VLM／LLM 進行單據分類及欄位萃取，另提供會計分類與人工覆核。
- **模型來源**：可在「模型設定」切換 Ollama 地端模型、內網 H200 vLLM 或雲端 Gemini／Claude／GPT；連線與金鑰設定見 [.env.example](.env.example)。H200 部署資訊見 [部署需求](docs/dev-notes/H200-VLM-佈署需求.md)。


### 預設與限制

| 設定 | 預設值 | 可調範圍／方式 |
| --- | --- | --- |
| 圖片長邊 | 1180 像素 | 640–2400；大圖等比例縮小，小圖不放大 |
| 每組重複次數 | 1 | 1–10 |
| 辨識逾時 | 600 秒 | 30–3600 秒，套用於版面分析及單據辨識子流程 |
| 每組先暖機一次 | 關閉 | 啟用後額外處理第一頁，不納入主要統計 |
| 單檔大小 | 100 MB | `VALIDATION_MAX_FILE_MB` |
| 資料集解壓總量 | 2 GB | `VALIDATION_MAX_TOTAL_MB`（以 MB 設定） |
| 檔案數 | 5,000 | `VALIDATION_MAX_FILES` |

驗證資料獨立保存在 `app/data/validation/`，與正式憑證資料庫分開。比較模型時應固定資料集、圖片長邊、辨識模式、重複與暖機條件，並保存程式／模型版本；評分方式見 [Agent 工作與驗證指南](docs/dev-notes/agent-workflow.md)。

## 資料庫檢視（管理員）

以管理員帳號登入後開啟 [資料庫檢視](http://127.0.0.1:8090/database.html)。可選擇已建立的「憑證與帳號」或「資料集驗證」資料庫，查看憑證、帳號、公司、品名科目對應與歷程，以及驗證資料集、批次、頁面及結果。

資料表與欄位以中文顯示，原始欄名可由提示查看；支援關鍵字、適用的日期／狀態篩選及每頁 25、50、100 筆，長文字與 JSON 可在側邊面板檢視。憑證以憑證日期篩選，驗證日期依台灣時間計算。此功能為唯讀，密碼雜湊與 salt 由後端遮蔽且不納入搜尋，API 僅開放管理員。


## 目錄與文件

- `app/`：正式程式碼、WebUI 與執行期資料目錄。
- `tests/`：離線契約與功能回歸測試。
- `scripts/`：驗證入口、帳號維護與資料處理工具。
- [開發線上手冊](docs/manual/index.html)：瀏覽器開啟的單一自包含手冊。
- [目前系統架構](docs/dev-notes/architecture.md)：FastAPI、SQLite、辨識及審核資料流。
- [Agent 工作與驗證指南](docs/dev-notes/agent-workflow.md)：測試範圍、部署核對與保存結果評分。
- `archive/phase0_poc_flask/`：已封存的 Flask／Gemini POC；歷史說明見 [POC 架構](docs/dev-notes/architecture-phase0.md)。
- `ref/`：本機參考文件與合約資料，另行保存。
