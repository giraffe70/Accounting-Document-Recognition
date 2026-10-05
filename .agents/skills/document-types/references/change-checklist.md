# 單據類型變更檢查表

所有相對路徑均以專案根目錄為基準；依變更內容判斷需要修改哪些檔案，避免為同步而改動無關程式。

## 主要契約與消費者

- `app/core/doc_types.py`：`DOC_TYPES` 的 key、label、aliases、fields、`has_items`／`has_entries`、`supporting`、`extra`；`ROUTER_LABELS` 自動產生；`NO_VLM_BOX_TYPES` 決定 prompt 是否要求 VLM 輸出 boxes。
- `app/core/doc_router.py`：`ROUTER_PROMPT` 的易混類型界線；`_menu()` 自動納入新 key，但文字規則與 aliases 仍需人工檢查。`classify()` 逐 key 掃描回應，留意新舊 key 子字串與別名碰撞。
- `app/core/pipeline.py`：`prepare_image()`、`extract_for_type()`、`_build_voucher()` 消費類型契約，部分類型有專用後處理；只檢查影響並由主代理處理核心辨識修改。
- `app/models/schemas.py`、`app/main.py`、`app/static/js/app.js`：檢查 API 與前端對 `doc_type_key`／動態欄位的假設。前端欄位由契約驅動，但可能有類型專用邏輯。
- `scripts/build_agents_section.py`：`GROUP`、`TASK`、`ORDER` 是硬編碼字典／順序，新 key 未加入會無法產生手冊；另檢查章節內固定「17 種專屬 + 1 種通用」文案與 `docs/manual/index.html` 的再生結果。
- `analysis/evaluate_validation.py`：`TYPE_LABELS` 映射模型顯示名到 key。`analysis/export_accuracy_report.py` 的 `DOC_TYPE_LABELS` 與 `scripts/build_full_image_validation_report.py` 的 `TYPE_LABELS` 亦需檢查，避免報表漏類型或顯示不一致。
- `scripts/score_full_image_validation.py` 內建 40 張 gold 與已保存驗證輸出只涵蓋原有樣本；新類型不會自動得到代表性評分。其他分析/報告腳本若含類型清單、標籤或假設，以 `rg -n 'DOC_TYPES|TYPE_LABELS|DOC_TYPE_LABELS|doc_type_key|ORDER' app scripts analysis tests` 再確認。

## `NO_VLM_BOX_TYPES` 判準

多欄、密集或多聯表單若 VLM 座標容易混入欄位值，考慮加入此集合；該類 prompt 不要求 VLM boxes，`pipeline.py` 依 OCR 行框精修。普通版面保留 VLM+OCR 路徑。判準需由真實正例及近似負例支撐，不憑類型名稱猜測。

## 回歸

1. 為新/變更類型準備至少一個正例與易混類型近似負例，包含預期 `doc_type_key` 與核心欄位；若缺資料，明確記錄未驗證範圍。
2. 不啟動模型的契約檢查可從專案根目錄執行：

   ```powershell
   python -c "from app.core import doc_types as d; assert 'other' in d.DOC_TYPES; assert all(t['label'] and t['fields'] for t in d.DOC_TYPES.values()); [d.build_extract_prompt(k) for k in d.DOC_TYPES]; print(len(d.DOC_TYPES), 'types OK')"
   python -m unittest discover -s tests
   ```

3. 有人工確認、可用的模型與成本授權時，再測正例、近似負例與全類型代表樣本；逐類列出誤判、欄位遺失及 `other` 回退率。不能用上述無模型測試替代全類型辨識回歸。
4. `python scripts/build_agents_section.py` 會改寫 `docs/manual/index.html`；只有實際類型變更且需要同步文件時執行。`analysis/evaluate_validation.py` 會重寫 gold 輸出，不能為回歸直接在來源目錄執行。
