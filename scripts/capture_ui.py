"""以 Playwright 自動登入並逐畫面截圖，供測試報告使用。

先前的 capture_web.ps1（無頭 Edge 直接開網址）無法登入，本系統所有畫面都需
cookie 驗證，故改用可操作瀏覽器的 Playwright：填帳密 → 切分頁 → 點開憑證
→ 等影像與彩色框載入 → 截圖。UI 改版後重跑即可更新報告截圖。

用法：python scripts/capture_ui.py [--url http://127.0.0.1:8090]
輸出：docs/report/shots/*.png
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/report/shots"
URL = "http://127.0.0.1:8090"
for a in sys.argv[1:]:
    if a.startswith("--url="):
        URL = a.split("=", 1)[1]

SHOTS = []


def main():
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1.5)
        pg.goto(URL, wait_until="networkidle")

        def shot(name, note):
            pg.wait_for_timeout(700)
            f = OUT / f"{name}.png"
            pg.screenshot(path=str(f))
            SHOTS.append((name, note))
            print(f"  {name:<22} {note}")

        # ① 登入畫面
        pg.wait_for_selector("#loginBtn", state="visible", timeout=15000)
        shot("01_login", "登入畫面：一般使用者與系統管理員、未登入無法使用任何功能")

        pg.fill("#loginUser", "admin")
        pg.fill("#loginPass", "admin123")
        pg.click("#loginBtn")
        pg.wait_for_selector("#loginOverlay", state="hidden", timeout=20000)
        pg.wait_for_timeout(1500)

        # ② 匯入頁（含掃描來源目錄）
        shot("02_import", "① 匯入：拖拉上傳、選整個資料夾、伺服器端掃描來源目錄")

        # 舊版實機畫面（歷史流程快照）：透過舊 pending stage tab 擷取佇列
        pg.click('.stage-tab[data-stage="pending"]')
        pg.wait_for_timeout(1800)
        shot("03_queue", "舊版實機畫面（歷史流程快照）：待審佇列顯示憑證號、單據種類、公司、日期與總額")

        # ④ 審核畫面（點第一筆，等影像與框載入）
        rows = pg.query_selector_all("#listBody tr")
        if rows:
            rows[0].click()
            pg.wait_for_timeout(3500)
            shot("04_review", "審核與編輯：左為掃描影像與彩色欄位框，右為可編輯欄位")
            # 捲到分錄區
            try:
                pg.eval_on_selector("#entriesSection, #itemsSection", "e => e.scrollIntoView({block:'center'})")
                pg.wait_for_timeout(900)
                shot("05_entries", "明細與會計分錄：借貸科目、平衡檢查")
            except Exception:
                pass

        # ⑤ 引擎狀態列（底部固定）
        pg.eval_on_selector("#engineBar", "e => e.scrollIntoView()")
        pg.wait_for_timeout(600)
        el = pg.query_selector("#engineBar")
        if el:
            el.screenshot(path=str(OUT / "06_enginebar.png"))
            SHOTS.append(("06_enginebar", "引擎狀態列：OCR／VLM／LLM 即時狀態與地端模型佔用"))
            print("  06_enginebar           引擎狀態列")

        # ⑥ 科目表
        pg.evaluate("document.querySelector('#accountsBtn').click()")
        pg.wait_for_timeout(1500)
        shot("07_accounts", "客製會計科目表：分公司存放，自動分類依歸屬公司取用")
        pg.evaluate("document.querySelector('#closeAccounts').click()")
        pg.wait_for_timeout(500)

        # ⑦ 日記帳
        pg.evaluate("document.querySelector('#journalBtn').click()")
        pg.wait_for_timeout(2000)
        shot("08_journal", "舊版實機畫面（歷史流程快照）：日記帳當時彙整已核定憑證分錄與借貸合計")
        b.close()

    print(f"\n共 {len(SHOTS)} 張 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
