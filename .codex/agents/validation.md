# 設定驗證與路由檢查

驗證日期：2026-10-05。新增設定依 OpenAI 官方 [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) 與 [Build skills](https://learn.chatgpt.com/docs/build-skills) 格式建立：專案代理位於 `.codex/agents/*.toml`，專案 Skill 位於 `.agents/skills/*/SKILL.md`。

模型分開設定為 `model = "gpt-6-luna"` 與 `model_reasoning_effort = "high"`；`gpt-6-luna-high` 不作為模型 ID。Scout 設定 `read-only`，Worker 繼承主代理的 sandbox。實際 sandbox 與工具權限仍以執行時設定為準，唯讀行為另由代理指令限制。

## 四種人工路由情境

| 情境 | 應由誰處理 | 人工檢查結果 |
| --- | --- | --- |
| 查找 `app/static/database.html` 中一段提示文字 | 主代理 | 單檔簡單搜尋，不啟動子代理 |
| 整理資料庫檢視頁表頭在 HTML、JS 與顯示 metadata 間的來源 | economy-scout | 跨檔唯讀探索；回報符號、行號與證據，遇核心停止條件即交回 |
| 在指定的靜態資料庫檢視頁修改提示文字與版面 | economy-worker | 限明確檔案和驗收條件；涉及 SQL、權限或 API 契約即交回 |
| 修改 `pipeline.py`、驗證排程、OCR/VLM 或會計借貸判定 | 主代理 | Scout 可唯讀整理入口/呼叫鏈及失敗證據；修復設計、會計判定及執行由主代理處理，Worker 停止修改 |

代理描述用於選擇角色，沒有新增「所有任務都必須委派」的規則。需要委派時給出明確問題、檔案範圍與交付項；不要把主代理已完成的探索再交給子代理重做。

## 已完成檢查

- 兩份 TOML 解析成功，必要欄位、檔名/name 一致、模型及 reasoning effort 檢查通過。
- 兩份 Skill YAML frontmatter、資料夾/name 一致性及 reference 相對連結檢查通過。
- `model-validation/SKILL.md` 為 15 行，`document-types/SKILL.md` 為 12 行；詳細命令與同步清單按需載入。
- 以 `python -X utf8 <skill-creator>/scripts/quick_validate.py <skill-path>` 驗證兩個 Skill，均回報 `Skill is valid!`。UTF-8 模式解決 Windows 預設 CP950 的讀取錯誤。
- 已核對參考的程式來源路徑存在，執行無模型的契約檢查，18 個類型皆可產生萃取提示。

初版只建立設定與說明；後續優化新增根目錄 `AGENTS.md`、委派協定、統一驗證入口與合成資料回歸。入口現為 `scripts\verify.bat -Scope check` 或 `-Scope all`，使用 pdfscan312 / UTF-8。兩個 Skill 保持 15/12 行。低成本 worker 已用於獨立離線測試實作；自訂角色的執行時載入與實際 Token 節省仍需後續任務確認。未執行真實辨識批次或修改来源 gold。

後續驗收：Python 3.12.14，35 個離線測試通過，涵蓋契約、Router、日期/金額/JSON、CSV/全圖評分 CLI、明示 gold 匯出、輸入不變、SQLite 唯讀與連線釋放；兩個 Skill 的官方 quick validator 皆通過。Git 保留優化前程式基準，新的評分結果不會覆寫歷史輸出。
