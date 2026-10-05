# 既有驗證路徑與限制

所有相對路徑均以專案根目錄為基準。先讀腳本現況，以下為建立本 Skill 時的索引，不是可直接執行所有命令的授權。

## 已保存的雙模型 CSV 比較

- `analysis/evaluate_validation.py` 需明示 `--run NAME=CSV_DIR` 恰好兩組、`--gold` 凍結 JSON、`--dataset-id` 與 `--output-dir` 新目錄。CSV 資料夾需含 `vouchers.csv`、`fields.csv`、`items.csv`、`entries.csv`；以 `(source, page)` 對齊，拒絕重複鍵與無共同頁面。
- 評分只讀 gold，輸出新目錄的 `validation_comparison.csv`、`validation_summary.json`，包含輸入與評分程式 SHA-256。不再組合或覆寫 `gold_standard.json/csv`，亦不自動沿用歷史 run ID。
- 可選 `--database <DB> --run-id NAME=ID` 兩組以唯讀核對資料集與批次設定；缺 metadata 時 `comparability_verified=false`，需人工核對 CSV 實際來源、辨識模式及條件。
- `--deduplicate` 要求全部共同頁面有原圖；相同 SHA-256 按來源/頁碼排序固定代表頁，群內 gold 衝突拒絕執行。沒有原圖證據時 `unique_image_pages=null`；去重與原始分母明確分列。已保存的 `analysis/validation_summary.json`、`analysis/validation_comparison.csv` 是歷史結果，不是新批次。
- `analysis/diagnose_runs.py` 是 trace 初步診斷，直接讀 `app/data/validation/validation.db` 中兩個硬編碼 run ID 的 `trace_json`。只對應那兩批；其他批次應先找實際 run ID 與 trace，不直接沿用結論。
- `analysis/export_accuracy_report.py` 仍是歷史 qwen/gemma CSV 報告產生器，預期原有 gold CSV 路徑及欄位；不自動套入任意新模型結果。
- gold 組合另由 `analysis/prepare_gold.py --base <JSON> --override <JSON> --output-dir <新目錄>` 執行；只有明確要求整理已覆核標註時使用，不屬自動評分。base 可重複指定且不允許重複鍵，override 明示順序，輸出 JSON 與來源雜湊，不修改來源。

## 40 張全圖辨識評分

- `scripts/score_full_image_validation.py --evidence <JSON> --builtin-gold --dataset-id <ID> --run-id <ID> --output-dir <新目錄>` 明示使用腳本內原有 40 張覆核 gold；其他樣本改用 `--gold <JSON>`，格式為圖片檔名到欄位物件的映射。輸出 `scored_evidence.json` 含來源識別與雜湊，不覆寫舊結果；來源 basename 碰撞會拒絕。
- `scripts/run_full_image_validation.py` 會呼叫辨識流程、寫工作圖與 evidence，`--force` 會重算；這不是評分命令，不得由本 Skill 自動執行。`--render-only` 仍會寫視覺化檔案。

## 傳票／會計分錄評估

- `scripts/eval_dataset.py` 從 `docs/dev-notes/dataset-manifest.json` 選 `test` 案例，使用 `ref/TEST_DATA`，寫 `docs/dev-notes/eval-result.json` 與 `docs/dev-notes/測試集比對結果.md`；它會呼叫完整辨識與分類。`--all` 包含訓練組，不能用於驗收；`--force` 忽略快取。不得把此腳本當成無成本、唯讀的評分命令。
- 報告裡的科目名稱等價映射 `EQUIV` 是暫定假設；不能把映射後「一致」直接稱為已核定的會計正確性。
- 該流程的傳票答案由辨識產生；未經人工覆核的答案只能評估流程一致性，不能代替凍結的人工 gold。

## 輸出格式

完整命令與 UTF-8/pdfscan312 環境見專案 `docs/dev-notes/agent-workflow.md`；離線評分回歸：`scripts\verify.bat -Scope scoring`。

簡報：資料集與批次識別、gold 與 evidence 雜湊、共同頁數／唯一影像數／gold 覆蓋率、成功與錯誤數、各指標分子和分母、模型間分歧、對 gold 的正確率、偏差與不可比條件。標明報告生成時點及腳本版本或雜湊。
