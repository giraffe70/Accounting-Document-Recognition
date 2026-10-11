# 輸入預算準備

此工具準備 Qwen2.5-VL 輸入預算所需的 tokenizer 資產與本機 Ollama profile。只取官方 revision `cc594898137f460bfe9f0759e9844b3ce807cfb5` 的 `tokenizer.json`、`tokenizer_config.json`，以及 Ollama 版本、模型標籤與 `show` metadata；不下載模型權重。資產與 profile manifest 由準備函式寫入指定目錄。

在專案的 `pdfscan312` Python 3.12 環境，以 UTF-8 安裝依賴並執行：

```powershell
& "$env:USERPROFILE/Miniconda3/envs/pdfscan312/python.exe" -m pip install tokenizers==0.22.2
& "$env:USERPROFILE/Miniconda3/envs/pdfscan312/python.exe" -B -X utf8 scripts/prepare_token_budget.py --model qwen2.5vl:7b
```

需要指定 Ollama 位置或資產目錄時，可加上 `--backend-url http://localhost:11434` 與 `--asset-dir <目錄>`。自訂資產目錄須在準備工具及服務的環境都設定 `PDFSCAN_TOKENIZER_DIR` 為同一絕對路徑。預設為被 Git 忽略的 `app/data/token_budget/`。CLI 成功時會輸出 `ready`、模型名稱與 profile ID；失敗時回傳 exit code 1，錯誤輸出只含錯誤類型。若準備失敗，禁止開始新批次；runtime 會提供設定未就緒的具體原因。

完成準備後，在現有服務終端按 Ctrl+C，確認停止，再手動執行 `start_webui.bat`。登入並開啟 `/api/validation/runtime`，確認 `ready=true`、`schema_ready=true`、`restart_required=false`、`token_budget.ready=true` 且 `loaded_version` 等於 `disk_version`。按 Ctrl+F5 載入 `/validation.html`，建立新批次。舊批次不回填、不重判，也不能以原批次重跑驗收新契約。

目前操作約束：context 維持 8192；保留原圖與高解析 OCR。先以本機 tokenizer 計算聊天內容及已核對模板，Schema 另外保守預留，圖片依 Ollama 0.35.0 的已核對 Qwen 處理器計算視覺網格。特殊 token 另有模板及圖片預留；這是有保守餘裕的輸入預算，不能宣稱與後端 token 數完全相等。模型 digest、模板、服務版本、資產雜湊或 tokenizer 版本不一致時，明示未就緒，須重新準備；其他未核對後端不採猜測估算。

送出條件為 `輸入預算 + 階段回答預留 + 512 <= 8192`，回答預留固定為 Router 2048、擷取 4096、補讀 2048。先精簡為一份 OCR 行原文及整數位置，再評估目前尺寸、2048、1800、1440、1180、1024、896 長邊；不放大，不縮減回答預留。最低視覺網格仍放不下時，回報 `context_budget_exhausted`（輸入預算不足），保留原文，模型不會被呼叫。純文字模式不加入圖片。Schema 降級及第二輪修復重新計算；修復片段最多 1024 字元，放不下可省略片段，完整回覆仍保留於 trace。

`recognition_diagnostics` 保存預算分項、候選尺寸、最終圖片雜湊、採用模式與補讀錯誤摘要。完整 OCR、原始回覆及後端回報的 token／結束原因在 trace，圖片 base64 不寫入。模型輸入 artifact 與實際送出 bytes 相同，原圖、OCR 與座標校準限制不變。預算器就緒不會啟用尚未通過校準的模型定位框。

首讀有效、補讀失敗時維持執行 `succeeded`，品質加上 `supplement_failed`；缺總額、有效分錄或內容仍為 `insufficient`，其他待核對情況為 `needs_review`。縮圖增加 `input_downscaled_for_budget` 與前後尺寸。補讀開始前保存原子 `first_read_checkpoint.json`；父流程只在補讀中止／逾時、來源及版本吻合、原契約有效且有實際內容時恢復首讀。取消、首讀失敗、全空或契約／版本不符不製造成功。手動值不被覆寫。

JSON、正式憑證及驗證 `documents[]` 保留同一摘要；CSV 相容附加 `recognition_diagnostics_json`，`pages.csv` 頁面耗時只列一次。這份準備流程不啟動真實推論，也不使用私有圖片或 gold。離線驗收只證明功能與契約，縮圖後的實際正確率仍需固定 gold 分母及人工覆核。
