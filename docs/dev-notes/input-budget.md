# 輸入預算準備

此工具準備 Qwen2.5-VL 輸入預算所需的 tokenizer 資產與本機 Ollama profile。只取官方 revision `cc594898137f460bfe9f0759e9844b3ce807cfb5` 的 `tokenizer.json`、`tokenizer_config.json`，以及 Ollama 版本、模型標籤與 `show` metadata；不下載模型權重。資產與 profile manifest 由準備函式寫入指定目錄。

在專案的 `pdfscan312` Python 3.12 環境，以 UTF-8 安裝依賴並執行：

```powershell
python -m pip install -r requirements.txt
python -B -X utf8 scripts/prepare_token_budget.py --model qwen2.5vl:7b
```

需要指定 Ollama 位置或資產目錄時，可加上 `--backend-url http://localhost:11434` 與 `--asset-dir <目錄>`。CLI 成功時會輸出 `ready`、模型名稱與 profile ID；失敗時回傳 exit code 1，錯誤輸出只含錯誤類型。

完成準備後，重新啟動服務：手動執行 `start_webui.bat`。確認 runtime 已就緒後按 Ctrl+F5，再開始新批次，讓新頁面載入最新 runtime profile。

目前操作約束：context 維持 8192；保留原圖 OCR。首讀補讀失敗須標記 `needs_review` / `insufficient`。縮圖候選尺寸依序為 2048、1800、1440、1180、1024、896。這份準備流程不啟動真實推論，也不使用私有圖片或 gold 樣本。
