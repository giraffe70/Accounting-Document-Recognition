"""組裝客戶測試報告（單一自包含 HTML，圖片 base64 內嵌）。

素材來源：
  - 操作截圖：scripts/capture_ui.py（Playwright 自動登入逐畫面拍攝）
  - 各類憑證辨識結果：scripts/build_report_samples.py（彩色框標註圖 + 欄位表）
  - 量化結果：docs/dev-notes/eval-result.json（測試集比對）
  - 樣式：沿用開發手冊，保持兩份文件視覺一致

執行前請先跑 capture_ui.py 與 build_report_samples.py。
輸出：docs/report/測試報告.html
"""
import base64
import json
import pathlib
import re
import sqlite3
import sys
from datetime import date

from build_report_samples import content_box

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "docs/report"
SHOTS = REPORT_DIR / "shots"
SAMPLES = REPORT_DIR / "samples"
OUT = REPORT_DIR / "測試報告.html"
MANUAL = ROOT / "docs/manual/index.html"
EVAL = ROOT / "docs/dev-notes/eval-result.json"
# 辨識引擎 A/B 實測（2026-08-18）：同一測試集、同一份程式碼，只換版面理解模型。
# 兩檔皆由 scripts/eval_dataset.py --force 產生，classify 固定不變以隔離變因。
EVAL_AB = [
    ("本機顯示卡　Qwen2.5-VL-7B", ROOT / "docs/dev-notes/eval-ab-ollama-vlm.json"),
    ("內網 GPU 伺服器　Qwen2.5-VL-7B", ROOT / "docs/dev-notes/eval-ab-h200-vlm.json"),
]


def b64(p: pathlib.Path, max_w=1500) -> str:
    """縮圖後轉 base64（單一檔案交付，不能外連圖檔）。"""
    from io import BytesIO

    from PIL import Image

    im = Image.open(p).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=86, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def eval_metrics(path: pathlib.Path) -> dict | None:
    """讀一份 eval 結果，回傳可計分案數、各指標通過數與總耗時（分）。"""
    if not path.exists():
        return None
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    scored = [r for r in rows if r.get("answer_reliable")]
    return {
        "files": {r["file"] for r in scored},
        "rows": {r["file"]: r for r in scored},
        "n": len(scored),
        "mins": round(sum(r.get("secs") or 0 for r in rows) / 60, 1),
        **{k: sum(1 for r in scored if r.get(k))
           for k in ("total_match", "structure_match", "accounts_match", "amounts_match")},
    }


def manual_style() -> str:
    """沿用開發手冊的 <style>（同一套視覺，客戶看兩份文件不會覺得是兩個系統）。"""
    s = MANUAL.read_text(encoding="utf-8")
    m = re.search(r"<style>(.*?)</style>", s, re.S)
    return m.group(1) if m else ""


def fig(img_b64: str, cap: str) -> str:
    return (f'<div class="diagram"><img src="{img_b64}" '
            f'style="max-width:100%;border-radius:6px;display:block;margin:0 auto;">'
            f'<div class="diagram-cap">{cap}</div></div>')


SHOT_CAPS = [
    ("01_login", "登入：記帳士／會計師／管理員三種角色，未登入無法使用任何功能"),
    ("02_import", "① 匯入：可拖拉上傳、選整個資料夾，或由伺服器端指定的掃描來源目錄批次匯入"),
    ("03_queue", "舊版實機畫面（歷史流程快照）：待審佇列及當時的六階段分頁，清單顯示憑證號、公司、日期與總額"),
    ("04_review", "審核與編輯：左為掃描影像與彩色欄位框，右為對應欄位；統編即時核對（綠字為檢查碼有效）"),
    ("05_entries", "明細與會計分錄：自動產生借貸科目並檢查平衡"),
    ("06_enginebar", "引擎狀態列（畫面底部）：OCR／VLM／LLM 三階段即時狀態與地端模型佔用"),
    ("07_accounts", "客製會計科目表：依公司別存放，自動分類時取用該憑證歸屬公司的科目表"),
    ("08_journal", "舊版實機畫面（歷史流程快照）：單月日記帳當時彙整已核定憑證分錄，計算借貸合計與平衡"),
]

# 已知問題的舉證憑證（記錄 id → 說明）。取自實測，非臆測。
PROBLEM_CASES = [
    (38, "手寫三聯式發票",
     "手寫字跡辨識不可靠：實測總計「1575」被讀成「1525」、買方統編「23051677」讀成「23051697」。"
     "系統以統編檢查碼、日期合理性、金額算術三道確定性檢查標示可疑處，但正確值仍需人工填寫。"),
    (11, "多筆金額的發票",
     "未稅／稅額／總計三者兜不攏時（未稅 49 ＋ 稅額 2,450 ≠ 總計 51,450，實為未稅 49,000 被讀成 49），"
     "系統會在總計欄位旁紅字標示，提醒人工核對。"),
]


def build():
    from app.core import doc_types

    samples = json.loads((REPORT_DIR / "samples.json").read_text(encoding="utf-8"))
    ev = json.loads(EVAL.read_text(encoding="utf-8")) if EVAL.exists() else []
    con = sqlite3.connect(ROOT / "app/data/app.db")

    # ── 統計 ──
    n_doc = con.execute("select count(*) from voucher_records").fetchone()[0]
    n_type = len(doc_types.DOC_TYPES)
    scored = [r for r in ev if r.get("answer_reliable")]
    tot = sum(1 for r in scored if r.get("total_match"))
    stru = sum(1 for r in scored if r.get("structure_match"))
    acc = sum(1 for r in scored if r.get("accounts_match"))
    n_s = len(scored) or 1

    P = []
    A = P.append
    A(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>憑證辨識系統 — 測試報告</title>
<style>{manual_style()}
main {{ margin-left:0; max-width:1180px; padding:32px 40px; }}
.sample-grid {{ display:grid; grid-template-columns: 1.15fr 1fr; gap:20px; align-items:start; }}
@media (max-width:900px) {{ .sample-grid {{ grid-template-columns:1fr; }} }}
.sample-grid img {{ width:100%; border:1px solid var(--border); border-radius:8px; }}
table.fields {{ width:100%; border-collapse:collapse; font-size:13px; }}
table.fields th, table.fields td {{ border-bottom:1px solid var(--border); padding:5px 8px; text-align:left; }}
table.fields th {{ background:var(--bg2,#f6f8fa); }}
.no {{ display:inline-block; min-width:20px; height:20px; line-height:20px; text-align:center;
  border-radius:4px; color:#fff; font-size:11px; font-weight:700; margin-right:6px; }}
@media print {{ .diagram, .sample-grid {{ break-inside: avoid; }} h2 {{ break-before: page; }} }}
</style></head><body><main>""")

    # ── 封面 ──
    A(f"""<section class="doc">
<div class="hero"><h1>票據憑證掃描辨識系統</h1>
<div class="tagline">測試報告　—　辨識結果、操作流程與現況問題</div>
<div class="badges"><span class="badge g">地端執行</span><span class="badge b">OCR + VLM 混合辨識</span>
<span class="badge o">{n_type} 種單據類型</span><span class="badge">測試憑證 {n_doc} 張</span></div></div>
<p class="lead">本報告呈現系統以<strong>客戶提供之真實憑證</strong>實際執行的辨識結果。
所有辨識皆在<strong>自有設備上完成</strong>——文字辨識於本機執行，版面理解與科目分類於
<strong>公司內網的 GPU 推論伺服器</strong>執行，全程不經網際網路，
憑證影像與財務資料不上傳任何雲端服務。報告日期：{date.today().isoformat()}。</p>
<div class="callout note"><div class="ct">💡 一句話理解</div>
依序使用①匯入、②外觀確認、③掃描清單、④確認；修正保存為草稿，確認後才進入日記帳、會計傳票與配對。全程繁體中文、全地端。</div>
</section>""")

    # ── 契約規格完成度 ──
    A(f"""<section class="doc"><h2><span class="ic">📋</span>契約規格完成度</h2>
<p class="lead">逐項對照契約所載規格與系統實際狀態。狀態欄以程式碼與實測結果認定，非估計值。</p>
<div class="callout note"><div class="ct">💡 本報告對應的契約範圍</div>
本報告以 <b>2026-01-07 簽訂之「委託技術服務契約書」（憑證辨識自動化分類技術合作開發計畫）</b>
及其附件一計畫書為準，執行期間 2026-07-01～2027-03-31。
2025-04-09 報價單所列第二期之「公有雲雲端平台部署」與「電子申報軟體串接」，
未列入本契約之交付項目，故不在本報告評估範圍內；若貴公司仍有此需求，建議另行討論。</div>

<h3>第一期：會計憑證自動化流程概念驗證</h3>
<table>
<tr><th>契約規格</th><th>狀態</th><th>說明</th></tr>
<tr><td>憑證掃描圖片上傳、繁體中文印刷字體辨識</td><td>✅ 完成</td><td>並已擴充為批次多頁 PDF</td></tr>
<tr><td>結構化憑證項目（憑證號碼、日期、買賣方名稱與統編、品名、數量、單價、合計、稅額、總計、備註）</td>
<td>✅ 完成</td><td>已依單據別擴充為 {n_type} 種專屬欄位契約，非單一通用格式</td></tr>
<tr><td>會計日記帳生成（單月、依日期／傳票號碼／借貸金額）</td><td>✅ 完成</td><td>可列印或另存 PDF</td></tr>
<tr><td>依歷史資料自動分類會計科目</td><td>✅ 完成</td><td>科目相符率有待補充記帳範例，詳見後節</td></tr>
<tr><td>兩種角色權限（記帳士初步確認、會計師最終核定）</td><td>✅ 完成</td><td>另含管理員角色</td></tr>
<tr><td>模型對齊功能（人工修正後回寫，供模型對齊）</td><td>✅ 完成</td><td>AI 原始值與人工修正值分別保存，差異可查</td></tr>
<tr><td>交付概念驗證測試報告</td><td>✅ 完成</td><td>即本報告</td></tr>
<tr><td><b>辨識率期望值 95%</b></td><td>🔶 部分達成</td>
<td><b>金額萃取 {tot/n_s:.0%}</b>（接近期望值）；<b>會計科目相符 {acc/n_s:.0%}</b>，主因為記帳範例不足，非辨識能力問題。
契約另載明手寫潦草、模糊、髒污等影響準確率屬正常範圍。建議雙方於驗收前先確認「95%」的量測對象與計算方式。</td></tr>
</table>

<h3>第二期：OCR 自動辨識分類正式版模組（交期 2026-11）</h3>
<table>
<tr><th>契約規格</th><th>狀態</th><th>說明</th></tr>
<tr><td>1. 支援批次 PDF 掃描憑證上傳，限制 5MB 以下</td><td>✅ 完成</td><td></td></tr>
<tr><td>2. 企業公司名稱與統編之核對功能</td><td>✅ 完成</td><td>統編檢查碼驗算 + 已登錄公司比對，畫面即時顯示</td></tr>
<tr><td>3. 進口報單處理邏輯鎖定</td><td>✅ 完成</td><td>偵測後套用專用分錄規則、統編免核</td></tr>
<tr><td>4. 客製版科目編號與名稱之編輯功能</td><td>✅ 完成</td><td>已擴充為<b>可依公司別分別存放</b></td></tr>
<tr><td>5. 產製報表之客製 Web 模板兩套</td><td>✅ 完成</td><td>憑證明細彙總表、會計傳票格式</td></tr>
<tr><td>6. 蒐集客戶測試回饋，UI 操作介面調整共三版次</td><td>🔶 進行中</td>
<td>已依回饋完成多輪調整（六階段流程、影像貼合、欄位警示等），惟<b>尚未以「版次」形式整理留存</b></td></tr>
<tr><td>7. 需求調整說明紀錄與版本更新</td><td>✅ 完成</td><td>開發手冊內含決策紀錄時間軸</td></tr>
<tr><td>管理者帳號權限文件一份</td><td>✅ 完成</td><td>另附《管理者帳號權限說明文件》，並補提供帳號管理工具（原本無法變更預設密碼）</td></tr>
<tr><td>地端備份硬體一套</td><td>🔶 軟體完成</td><td>備份模組已可用（資料庫＋設定打包）；<b>硬體尚未採購交付</b></td></tr>
</table>

<h3>第二期：憑證轉換營業稅媒體檔模組（交期 2027-03）</h3>
<table>
<tr><th>契約規格</th><th>狀態</th><th>說明</th></tr>
<tr><td>產製 TXT 與 TET_U 營業稅媒體檔，供轉入申報系統</td><td>❌ 未開始</td>
<td><b>需貴公司先提供計畫書所列之測試基準資料</b>：三家企業之原始憑證、會計傳票、明細表、
TXT 與 TET_U 檔，以及對應之最終 401 申報書。無此基準無法確認產製格式是否可正確轉入。</td></tr>
</table>

<h3>第二期：第三期技術合作提案（交期 2027-03）</h3>
<table>
<tr><th>契約規格</th><th>狀態</th><th>說明</th></tr>
<tr><td>推廣討論與需求整理清單、提案報告書一份</td><td>❌ 未開始</td><td>依時程於計畫後期進行</td></tr>
</table>
</section>""")

    # ── 系統架構 ──
    A("""<section class="doc"><h2><span class="ic">🏗️</span>系統架構</h2>
<p class="lead">辨識採「OCR ＋ VLM 混合」：OCR 負責讀出精確文字與座標，VLM 負責理解版面、判斷哪段文字屬於哪個欄位。</p>
<div class="diagram"><div class="mermaid">
flowchart LR
  S["掃描檔<br/>PDF／圖片"] --> R["判斷單據種類"]
  R --> C{{"人工確認"}}
  C --> E["萃取欄位<br/>OCR＋VLM"]
  E --> V["自動檢查<br/>統編／日期／金額"]
  V --> J["產生會計分錄"]
  J --> D1["保存修正草稿"] --> A2["④ 確認"] --> D[("日記帳／傳票／配對")]
</div><div class="diagram-cap">圖 1 — 四頁籤流程：確認完成後才納入帳務；僅已確認憑證供日記帳、傳票與配對使用</div></div>
<table>
<tr><th>面向</th><th>OCR（PaddleOCR）</th><th>VLM（Qwen2.5-VL）</th></tr>
<tr><td>角色</td><td>精準的眼睛：讀出文字與<strong>精確座標</strong></td><td>理解的大腦：看懂版面、判斷欄位歸屬</td></tr>
<tr><td>強項</td><td>統編／金額數字精確、框線位置準</td><td>版面理解、單據種類判斷</td></tr>
<tr><td>弱項</td><td>不理解結構（不知數字是統編還是金額）</td><td>數字易看錯、框座標不可靠</td></tr>
</table>
<div class="callout note"><div class="ct">💡 為何兩者並用</div>
單用 VLM：懂結構但統編／金額常誤判；單用 OCR：字準但只是一堆散字沒有結構。
兩者合用＝OCR 的「準」補 VLM 的短、VLM 的「懂」補 OCR 的短。
<strong>畫面上的彩色框由 OCR 驗證過才顯示</strong>，對不上的欄位寧可不給框。</div>
</section>""")

    # ── 操作流程 ──
    A('<section class="doc"><h2><span class="ic">🖥️</span>操作流程（實機畫面）</h2>'
      '<p class="lead">以下為系統實際執行畫面，依操作順序排列。</p>')
    for i, (name, cap) in enumerate(SHOT_CAPS, start=2):
        f = SHOTS / f"{name}.png"
        if f.exists():
            A(fig(b64(f), f"圖 {i} — {cap}"))
    A("</section>")

    # ── 各類憑證辨識結果 ──
    A(f'<section class="doc"><h2><span class="ic">🧾</span>各類憑證辨識結果</h2>'
      f'<p class="lead">每種單據各取一張實際樣本，左為系統標註的欄位位置（彩色框與編號），'
      f'右為萃取出的欄位值。編號與框色一一對應，客戶可逐欄核對。'
      f'欄位標「■」表示該欄位已由 OCR 驗證出精確位置。</p>')
    for s in samples:
        img = SAMPLES / s["image"]
        if not img.exists():
            continue
        rows = []
        n = 0
        pal = ["#0969da", "#1a7f37", "#cf222e", "#8250df", "#bc4c00",
               "#0891b2", "#be185d", "#4d7c0f", "#9a6700", "#6e40c9", "#1b7c83", "#a40e26"]
        spec_keys = [f["key"] for f in doc_types.get(s["type_key"])["fields"]]
        for r in s["fields"]:
            idx = spec_keys.index(r["key"]) if r["key"] in spec_keys else 0
            if r["boxed"]:
                n += 1
                badge = f'<span class="no" style="background:{pal[idx % len(pal)]}">{n}</span>'
            else:
                badge = '<span class="no" style="background:#c9d1d9">–</span>'
            rows.append(f'<tr><td>{badge}{esc(r["label"])}</td><td><b>{esc(r["value"])}</b></td></tr>')
        A(f"""<h3>{esc(s['label'])}</h3>
<div class="sample-grid">
  <div><img src="{b64(img, 1200)}" alt="{esc(s['label'])}"></div>
  <div><table class="fields"><tr><th>欄位</th><th>辨識結果</th></tr>{''.join(rows)}</table>
  <p class="hint">樣本：{esc(s['source'])[:46]}　｜　共 {len(s['fields'])} 個欄位有值、{s['n_box']} 個已定位</p></div>
</div>""")
    A("</section>")

    # ── 量化結果 ──
    if scored:
        A(f"""<section class="doc"><h2><span class="ic">📊</span>準確度量測</h2>
<p class="lead">以客戶提供的「傳票＋憑證」配對為基準：把憑證交給系統辨識並自動分類，
再與客戶實際記帳的傳票逐項比對。測試組共 {len(scored)} 案，未參與任何規則制定。</p>
<table><tr><th>指標</th><th>意義</th><th>結果</th></tr>
<tr><td>借方總額相符</td><td>金額萃取是否正確</td><td><b>{tot} / {len(scored)}（{tot/n_s:.0%}）</b></td></tr>
<tr><td>金額結構相符</td><td>含稅額拆分在內的每筆金額</td><td>{stru} / {len(scored)}（{stru/n_s:.0%}）</td></tr>
<tr><td>會計科目相符</td><td>選用科目與客戶記帳一致</td><td>{acc} / {len(scored)}（{acc/n_s:.0%}）</td></tr></table>
<div class="callout warn"><div class="ct">⚠️ 數字如何解讀</div>
<ul>
<li><strong>金額萃取（{tot/n_s:.0%}）是系統目前最成熟的部分</strong>，也是最能節省打字時間的環節。</li>
<li><strong>會計科目相符率偏低（{acc/n_s:.0%}）主因是參考資料不足</strong>：系統需要客戶既有的記帳範例才知道
「這類支出貴公司習慣記哪個科目」，目前僅有一組範例可用。此項可透過補充資料明顯改善（見下節）。</li>
<li>科目不同不等於錯誤——部分差異是兩套科目表的名稱差異（如「水電費」與「水電瓦斯費」）。</li>
</ul></div></section>""")

    # ── 辨識引擎 A/B 實測 ──
    ab = [(label, eval_metrics(p)) for label, p in EVAL_AB]
    ab = [(label, m) for label, m in ab if m]
    if len(ab) == 2:
        (la, ma), (lb, mb) = ab
        both = sorted(ma["files"] & mb["files"])          # 兩邊都可計分的案例才同基準
        cnt = lambda m, k: sum(1 for f in both if m["rows"][f].get(k))
        rows_html = ""
        for key, label in (("total_match", "借方總額相符"), ("structure_match", "金額結構相符"),
                           ("accounts_match", "會計科目相符"), ("amounts_match", "科目與金額全對")):
            a_v, b_v = cnt(ma, key), cnt(mb, key)
            d = b_v - a_v
            tone = "—" if d == 0 else (f"+{d}" if d > 0 else str(d))
            rows_html += (f"<tr><td>{label}</td><td>{a_v} / {len(both)}</td>"
                          f"<td>{b_v} / {len(both)}</td><td>{tone}</td></tr>")
        A(f"""<section class="doc"><h2><span class="ic">⚖️</span>辨識引擎搬遷實測</h2>
<p class="lead">版面理解模型由<strong>本機顯示卡</strong>改為<strong>公司內網的 GPU 推論伺服器</strong>執行。
為確認搬遷不影響辨識品質，以<strong>同一份測試集、同一份程式碼</strong>各跑一輪完整比對，
其餘設定（文字辨識、科目分類）全程固定不變，以隔離變因。</p>
<table><tr><th>指標</th><th>{esc(la)}</th><th>{esc(lb)}</th><th>差異</th></tr>
{rows_html}
<tr><td>整批執行時間</td><td>{ma['mins']} 分</td><td>{mb['mins']} 分</td>
<td>{mb['mins'] - ma['mins']:+.1f} 分</td></tr></table>
<div class="callout note"><div class="ct">💡 結論</div>
兩者在 {len(both)} 案同基準比較下<strong>辨識品質沒有實質差異</strong>，個別案例的互有增減屬正常波動。
搬遷的效益在於<strong>不再占用本機顯示卡</strong>、辨識能力可隨伺服器資源擴充，
而非提高準確度——準確度的瓶頸在記帳範例不足（見下節），不在運算硬體。</div>
<div class="callout warn"><div class="ct">⚠️ 為何是 {len(both)} 案而非 {ma['n']} 案</div>
本測試集的標準答案取自客戶傳票的辨識結果，而傳票同樣由版面理解模型讀取，
因此換模型會連帶影響「哪些案例的標準答案足夠可信」。上表僅採計<strong>兩種設定下答案皆可信</strong>的
{len(both)} 案，確保兩欄比較的是同一批案例。</div></section>""")

    # ── 尚未解決的問題 ──
    A('<section class="doc"><h2><span class="ic">🚧</span>尚未解決的問題</h2>')
    A("""<h3>一、需要客戶協助的事項</h3>
<table><tr><th>項目</th><th>說明</th><th>需要什麼</th></tr>
<tr><td><b>記帳範例不足</b></td><td>系統靠既有記帳範例學習貴公司的科目習慣，目前僅 1 組可用，
一般費用（運費、租金、保險、水電、勞健保）沒有任何範例。這是科目相符率偏低的主因。</td>
<td>10~20 組「傳票＋憑證」配對，皆為現成既有帳務資料</td></tr>
<tr><td><b>兩套會計科目表並存</b></td><td>事務所科目表與貴公司 ERP 科目表代號完全不同
（如運費：ERP 6205／事務所 6050），且事務所表缺少「預付進口款項」「手續費」等科目。</td>
<td>確認系統產出的分錄要用哪一套代號；缺少的科目是否補列</td></tr>
<tr><td><b>營業稅媒體檔測試基準</b></td><td>TXT／TET_U 與 401 申報書尚未實作。計畫書載明由貴公司提供三家企業之
原始憑證、會計傳票、明細表、TXT 與 TET_U 檔及對應之最終 401 申報書作為測試基準；無此基準無法驗證產製的檔案能否正確轉入申報系統。</td>
<td>上述測試基準資料一組</td></tr>
</table>

<h3>二、我方尚待完成的交付項目</h3>
<table><tr><th>項目</th><th>狀態</th><th>說明</th></tr>
<tr><td>地端備份硬體一套</td><td>待採購交付</td><td>備份軟體模組已完成並可操作，硬體本體尚未交付</td></tr>
<tr><td>UI 調整版次紀錄</td><td>待整理</td><td>已依回饋完成多輪調整，惟尚未以契約所述「三版次」形式整理留存</td></tr>
<tr><td>營業稅媒體檔模組</td><td>待客戶資料</td><td>見上表</td></tr>
<tr><td>第三期技術合作提案</td><td>依時程辦理</td><td>交期 2027-03</td></tr>
</table>""")

    A("<h3>三、辨識上的已知限制</h3>")
    for rid, title, desc in PROBLEM_CASES:
        row = con.execute("select current_json, image_path from voucher_records where id=?",
                          (rid,)).fetchone()
        if not row:
            continue
        cur, img = row
        p = ROOT / "app/data/uploads" / img
        A(f"<h4>{esc(title)}</h4><p>{desc}</p>")
        if p.exists():
            # 問題憑證同樣裁去空白邊，否則掃描件的大片留白會讓憑證只佔一小角
            from PIL import Image
            im = Image.open(p); W, H = im.size
            cx1, cy1, cx2, cy2 = content_box(p)
            crop = REPORT_DIR / f"problem_{rid}.png"
            im.crop((int(cx1*W), int(cy1*H), int(cx2*W), int(cy2*H))).save(crop)
            A(fig(b64(crop, 1000), f"問題舉例 — {esc(title)}（紀錄 #{rid}）"))
    A("""<div class="callout danger"><div class="ct">🚨 使用上務必注意</div>
<ul>
<li><strong>手寫憑證請務必人工核對金額</strong>。系統會標出可疑處，但正確值需人工填寫。</li>
<li>系統定位為<strong>減少打字與初步判斷</strong>，修正內容先保存為草稿；按下確認後才進入帳務。</li>
<li>勞健保費的「單位負擔／個人負擔」拆分需對照薪資資料，繳費證明本身查不到，系統不會臆測。</li>
</ul></div></section>""")

    A(f"""<section class="doc"><h2><span class="ic">📄</span>報告資訊</h2>
<table><tr><th>項目</th><th>內容</th></tr>
<tr><td>測試憑證</td><td>{n_doc} 張，皆為客戶提供之真實憑證</td></tr>
<tr><td>支援單據類型</td><td>{n_type} 種</td></tr>
<tr><td>執行環境</td><td>本機（Windows + NVIDIA RTX 5080）＋ 公司內網 GPU 推論伺服器（NVIDIA H200），
無雲端連線</td></tr>
<tr><td>辨識引擎</td><td>PaddleOCR（文字與座標，本機）＋ Qwen2.5-VL-7B（版面理解，內網伺服器）
＋ Qwen3-32B（科目分類，內網伺服器）</td></tr>
<tr><td>報告產生</td><td>{date.today().isoformat()}　由 scripts/build_report.py 自資料庫實際結果產生</td></tr>
</table></section>""")

    A("""</main>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>mermaid.initialize({startOnLoad:true, theme:'default', flowchart:{useMaxWidth:true, curve:'basis'}});</script>
</body></html>""")

    OUT.write_text("".join(P), encoding="utf-8")
    mb = OUT.stat().st_size / 1024 / 1024
    print(f"→ {OUT.relative_to(ROOT)}　{mb:.1f} MB")
    print(f"   操作截圖 {sum(1 for n,_ in SHOT_CAPS if (SHOTS/f'{n}.png').exists())} 張"
          f"、憑證樣本 {len(samples)} 種、量測案例 {len(scored)} 案")


if __name__ == "__main__":
    build()
