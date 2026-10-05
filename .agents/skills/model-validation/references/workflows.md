# 既有驗證路徑與限制

所有相對路徑均以專案根目錄為基準。先讀腳本現況，以下為建立本 Skill 時的索引，不是可直接執行所有命令的授權。

## 已保存的雙模型 CSV 比較

- `analysis/evaluate_validation.py` 從 `app/data/validation/result/validation-<run-id>-csv_<model>/` 讀取 `vouchers.csv`、`fields.csv`、`items.csv`、`entries.csv`，從 `analysis/gold_voucher_cases.json`、`gold_other_sources.json`、`gold_jpg.json` 與可選的 `gold_extra_review*.json` 組合 gold。它以 `(source, page)` 對齊，重複鍵會報錯。
- 此腳本把 qwen、gemma 的輸出目錄、兩個 run ID 及資料集 ID 硬編碼；還會重寫 `analysis/gold_standard.json`、`analysis/gold_standard.csv`、`analysis/validation_comparison.csv`、`analysis/validation_summary.json`。**不得在來源工作目錄直接執行**。要重新評分時，在隔離副本準備相同相對目錄與唯讀輸入，讓輸出只寫入副本；執行 `python analysis/evaluate_validation.py` 後核對來源 gold SHA-256 未變。缺任一批次或 gold 時停止並回報，不替換成別的 ID 猜測。
- 已保存輸出可直接閱讀：`analysis/validation_summary.json`、`analysis/validation_comparison.csv`、`analysis/validation_review.md`。摘要中的 `duplicate_image_groups` 依 `app/data/validation/validation.db` 與硬編碼資料集 ID 檢查影像 SHA-256；資料庫缺失時不能把空清單解釋成沒有重複。
- 現有 run ID 為 `f89d0bf3f54d4e93ab91e9b82c41beb5`、`e0b0cf8e8040411cbdb0b70d8d54f25e`，資料集 ID 為 `d7033a034f3840a0be62a4f71516b60b`；只適用於對應歷史批次。`unique_image_pages` 只是統計，`field_scores` 並未去除重複影像。要報去重準確率，需在隔離評估中先固定每個 SHA-256 的代表頁，檢查群內 gold 一致後再計分；缺原圖或有標註衝突時回報限制。
- `analysis/diagnose_runs.py` 是 trace 初步診斷，直接讀 `app/data/validation/validation.db` 中兩個硬編碼 run ID 的 `trace_json`。只對應那兩批；其他批次應先找實際 run ID 與 trace，不直接沿用結論。
- `analysis/export_accuracy_report.py` 從 `analysis/gold_standard.csv` 與 `analysis/validation_comparison.csv` 產生 PDF/DOCX，屬報告產生器；需先確定 CSV 對應同一批來源。

## 40 張全圖辨識評分

- `scripts/score_full_image_validation.py` 讀 `artifacts/full_image_validation/recognition_evidence.json`，使用**腳本內建** 40 張人工覆核 `GROUND_TRUTH`，寫 `artifacts/full_image_validation/scored_evidence.json`。命令：`python scripts/score_full_image_validation.py`。執行前確認 evidence 的來源及模型設定與欲評估批次相符，並記錄腳本和 evidence 的 SHA-256；它會覆寫 scored 輸出，不會改 gold。
- `scripts/run_full_image_validation.py` 會呼叫辨識流程、寫工作圖與 evidence，`--force` 會重算；這不是評分命令，不得由本 Skill 自動執行。`--render-only` 仍會寫視覺化檔案。

## 傳票／會計分錄評估

- `scripts/eval_dataset.py` 從 `docs/dev-notes/dataset-manifest.json` 選 `test` 案例，使用 `ref/TEST_DATA`，寫 `docs/dev-notes/eval-result.json` 與 `docs/dev-notes/測試集比對結果.md`；它會呼叫完整辨識與分類。`--all` 包含訓練組，不能用於驗收；`--force` 忽略快取。不得把此腳本當成無成本、唯讀的評分命令。
- 報告裡的科目名稱等價映射 `EQUIV` 是暫定假設；不能把映射後「一致」直接稱為已核定的會計正確性。
- 該流程的傳票答案由辨識產生；未經人工覆核的答案只能評估流程一致性，不能代替凍結的人工 gold。

## 輸出格式

簡報：資料集與批次識別、gold 與 evidence 雜湊、共同頁數／唯一影像數／gold 覆蓋率、成功與錯誤數、各指標分子和分母、模型間分歧、對 gold 的正確率、偏差與不可比條件。標明報告生成時點及腳本版本或雜湊。
