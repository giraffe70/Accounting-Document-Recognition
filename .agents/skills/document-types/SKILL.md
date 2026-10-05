---
name: document-types
description: 新增或調整單據類型、欄位契約或 Router 分類規則時使用；同步檢查辨識、手冊、報表映射與回歸樣本。
---

# 單據類型變更

以 `app/core/doc_types.py` 的 `DOC_TYPES` 欄位契約為起點，但先確認需求、實際單據與相近類型的界線；`app/core/doc_router.py` 的提示與別名可能需要同步。確認新類型是入帳依據或佐證附頁，以及 `NO_VLM_BOX_TYPES` 是否適用。

在實作前後檢查手冊產生器、分析報表的類型映射及現有測試資料的 gold 分布。準備清楚的正例與近似負例，驗證 Router 分類、欄位契約及全部類型的基本回歸。不要為了讓測試通過而改寫 gold；涉及 OCR/VLM、會計分類或借貸邏輯時由主代理處理。

同步檔案清單、密集表單判準與回歸命令見 [references/change-checklist.md](references/change-checklist.md)；實際修改時再讀。
