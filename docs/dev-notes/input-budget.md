# 輸入預算準備

此工具準備 Qwen2.5-VL 輸入預算所需的 tokenizer 資產與本機 Ollama profile。只取官方 revision `cc594898137f460bfe9f0759e9844b3ce807cfb5` 的 `tokenizer.json`、`tokenizer_config.json`，以及 Ollama 版本、模型標籤與 `show` metadata；不下載模型權重。資產與 profile manifest 由準備函式寫入指定目錄。

在專案的 `pdfscan312` Python 3.12 環境，以 UTF-8 安裝依賴並執行：

```powershell
& "$env:USERPROFILE/Miniconda3/envs/pdfscan312/python.exe" -m pip install tokenizers==0.22.2
& "$env:USERPROFILE/Miniconda3/envs/pdfscan312/python.exe" -B -X utf8 scripts/prepare_token_budget.py --model qwen2.5vl:7b
```

需要指定 Ollama 位置或資產目錄時，可加上 `--backend-url http://localhost:11434` 與 `--asset-dir <目錄>`。自訂資產目錄須在準備工具及服務的環境都設定 `PDFSCAN_TOKENIZER_DIR` 為同一絕對路徑。預設為被 Git 忽略的 `app/data/token_budget/`。CLI 成功時會輸出 `ready`、模型名稱與 profile ID；失敗時回傳 exit code 1，錯誤輸出只含錯誤類型。若準備失敗，禁止開始新批次；runtime 會提供設定未就緒的具體原因。

已核對的 Ollama 版本為 `0.35.0` 與 `0.40.4`。`0.40.4` 的官方 `LLAMA_CPP_VERSION` 釘選 `b11521`；已核對 runner 的 `--image-min-tokens 1024`、Qwen 的 4096-token 上限、28-pixel 網格及置中 `PAD_CEIL` 補邊，並將來源 URL 與 SHA-256 保存於 `model_coordinates.CHECKED_PROCESSORS`。這些規則與原有 `0.35.0` 的預算計算相容，不會自動啟用未校準的模型定位框。

若升級後出現 `backend identity or chat template changed; prepare again`，先確認新版本在上述清單內，再重新執行準備命令，完整重啟 Web 服務並重新載入頁面，確認 `/api/validation/runtime` 的 `token_budget.ready=true`、`restart_required=false`。例如從 `0.35.0` 升至 `0.40.4`，即使模型與模板沒變，也必須重建包含新服務版本的 manifest。未知版本須先核對官方來源並補上支援；不可直接修改 manifest 的版本或放寬身分檢查。

context 僅由環境變數 `OLLAMA_NUM_CTX` 設定，也可寫入 `.env`；已有的終端或系統同名環境變數優先於 `.env`。支援 `8192`、`12288`、`16384`，未設定時預設 `16384`。手動實測前，在 `.env` 明確設定 `OLLAMA_NUM_CTX=16384`（或要測的 `OLLAMA_NUM_CTX=12288`），並核對終端沒有其他值。在目前服務終端按 Ctrl+C 確認停止，再手動執行 `start_webui.bat`。登入並開啟 `/api/validation/runtime`，確認 `ready=true`、`schema_ready=true`、`restart_required=false`、`token_budget.ready=true`、`loaded_version` 等於 `disk_version`，以及 `token_budget.configured_context` 等於所選值；同時查看 `token_budget.supported_contexts`、`token_budget.input_limits`、`token_budget.policy_version`，並確認 `token_budget.hardware_verified=false`。`ready` 只代表設定及版本檢查通過，不代表硬體已驗證。

詞表 profile 維持 version 1，預算政策升為 version 2。context 改變不改變詞表身分；既有詞表資產不需重新下載或重新執行準備工具。設定確認後按 Ctrl+F5 載入 `/validation.html`，先使用另外建立的單頁測試資料集（保留原資料集），模型組合一組、重複一次、關閉預熱，建立新批次；排程仍為單一 worker。執行期間用 `ollama ps` 核對模型列出的 `CONTEXT`，並觀察 GPU/CPU 分配；用 `nvidia-smi` 觀察顯存，同時紀錄 context 設定、耗時與是否出現 OOM 或 CPU 分流。context 的輸入預算依 `輸入預算 + 階段回答預留 + 512 <= configured_context` 計算。硬體試跑通過後，再為原資料集建立新批次。

若 16384 發生 OOM 或出現新增 CPU 分流，手動將 `.env` 改為 `OLLAMA_NUM_CTX=12288`，按 Ctrl+C 停止服務後重新執行 `start_webui.bat`，確認 runtime 的 `configured_context=12288`，按 Ctrl+F5 後建立另一個新批次再觀察。系統不會自動降低 context。舊批次不回填、不重判，也不能以原批次重跑驗收新契約。

目前操作約束：單 worker 不變，保留原圖與高解析 OCR。先以本機 tokenizer 計算聊天內容及已核對模板，Schema 另外保守預留，圖片依 Ollama 0.35.0／0.40.4 的已核對 Qwen 處理器計算視覺網格。特殊 token 另有模板及圖片預留；這是有保守餘裕的輸入預算，不能宣稱與後端 token 數完全相等。模型 digest、模板、服務版本、資產雜湊或 tokenizer 版本不一致時，明示未就緒，須重新準備；context 設定變更本身不要求重新準備詞表。其他未核對後端不採猜測估算。

送出條件為 `輸入預算 + 階段回答預留 + 512 <= configured_context`，回答預留固定為 Router 2048、擷取 4096、補讀 2048。先精簡為一份 OCR 行原文及整數位置，再評估目前尺寸、2048、1800、1440、1180、1024、896 長邊；不放大，不縮減回答預留。最低視覺網格仍放不下時，回報 `context_budget_exhausted`（輸入預算不足），保留原文，模型不會被呼叫。純文字模式不加入圖片。Schema 降級及第二輪修復重新計算；修復片段最多 1024 字元，放不下可省略片段，完整回覆仍保留於 trace。

`recognition_diagnostics` 保存預算分項、候選尺寸、最終圖片雜湊、採用模式與補讀錯誤摘要。完整 OCR、原始回覆及後端回報的 token／結束原因在 trace，圖片 base64 不寫入。模型輸入 artifact 與實際送出 bytes 相同，原圖、OCR 與座標校準限制不變。預算器就緒不會啟用尚未通過校準的模型定位框。

首讀有效、補讀失敗時維持執行 `succeeded`，品質加上 `supplement_failed`；缺總額、有效分錄或內容仍為 `insufficient`，其他待核對情況為 `needs_review`。縮圖增加 `input_downscaled_for_budget` 與前後尺寸。補讀開始前保存原子 `first_read_checkpoint.json`；父流程只在補讀中止／逾時、來源及版本吻合、原契約有效且有實際內容時恢復首讀。取消、首讀失敗、全空或契約／版本不符不製造成功。手動值不被覆寫。

JSON、正式憑證及驗證 `documents[]` 保留同一摘要；CSV 相容附加 `recognition_diagnostics_json`，`pages.csv` 頁面耗時只列一次。這份準備流程不啟動真實推論，也不使用私有圖片或 gold。離線驗收只證明功能與契約，縮圖後的實際正確率仍需固定 gold 分母及人工覆核。
