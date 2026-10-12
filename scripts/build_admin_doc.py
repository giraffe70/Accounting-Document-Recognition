"""產生「管理者帳號權限說明文件」（契約交付項目之一）。

內容取自實際程式碼（auth.py 的角色定義、main.py 的權限中介層、前端 applyRole），
非另行撰寫的規格書——避免文件與系統行為不一致。
輸出：docs/report/管理者帳號權限文件.html
"""
import pathlib
import re
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/report/管理者帳號權限文件.html"
MANUAL = ROOT / "docs/manual/index.html"


def style() -> str:
    s = MANUAL.read_text(encoding="utf-8")
    m = re.search(r"<style>(.*?)</style>", s, re.S)
    return m.group(1) if m else ""


HTML = f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>管理者帳號權限說明文件</title>
<style>{style()}
main {{ margin-left:0; max-width:1080px; padding:32px 40px; }}
@media print {{ h2 {{ break-before: page; }} table {{ break-inside: avoid; }} }}
</style></head><body><main>

<section class="doc">
<div class="hero"><h1>管理者帳號權限說明文件</h1>
<div class="tagline">票據憑證掃描辨識系統　—　帳號、角色與權限管理</div>
<div class="badges"><span class="badge g">三種角色</span><span class="badge b">PBKDF2 密碼雜湊</span>
<span class="badge o">全地端</span></div></div>
<p class="lead">本文件說明系統的帳號種類、各角色可執行的操作範圍，以及管理員應執行的帳號維護作業。
內容依系統實際程式行為撰寫。文件日期：{date.today().isoformat()}。</p>
<div class="callout danger"><div class="ct">🚨 上線前必做</div>
系統首次啟動會自動建立三個<b>預設密碼</b>的種子帳號。<b>正式使用前務必全部變更</b>，
變更方式見下方〈帳號維護作業〉。未變更前請勿將系統開放給實際使用者。</div>
</section>

<section class="doc"><h2><span class="ic">👥</span>角色與權限</h2>
<p class="lead">系統採三種角色。權限在<b>後端強制檢查</b>（非僅隱藏畫面按鈕），
即使直接呼叫 API 也會被擋下。</p>
<table>
<tr><th>操作</th><th>記帳士<br>clerk</th><th>會計師<br>accountant</th><th>管理員<br>admin</th></tr>
<tr><td>登入系統、瀏覽憑證清單</td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td>上傳／匯入憑證、外觀確認、內容辨識</td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td>編輯欄位、影像旋轉裁切、自動分類科目</td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td>保存修正草稿</td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td><b>確認憑證</b></td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td><b>開發者備註（dev_note）</b></td><td>—</td><td>—</td><td>✓</td></tr>
<tr><td>日記帳、報表、列印／PDF</td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td><b>會計科目表：檢視</b></td><td>✓</td><td>✓</td><td>✓</td></tr>
<tr><td><b>會計科目表：儲存變更</b></td><td>—</td><td>—</td><td>✓</td></tr>
<tr><td><b>地端備份與下載</b></td><td>—</td><td>—</td><td>✓</td></tr>
<tr><td><b>系統組態</b>（模型連線、API 金鑰、掃描來源、雲端權限）</td><td>—</td><td>—</td><td>✓</td></tr>
<tr><td><b>修正回饋頁</b>（檢視 AI 原始值與人工修正差異）</td><td>—</td><td>—</td><td>✓</td></tr>
</table>
<div class="callout note"><div class="ct">💡 憑證確認權限</div>
三種登入角色皆可完成憑證確認。管理員另可使用開發者備註及管理功能。</div>
</section>

<section class="doc"><h2><span class="ic">🔐</span>安全設計</h2>
<table>
<tr><th>項目</th><th>作法</th></tr>
<tr><td>密碼儲存</td><td>PBKDF2-HMAC-SHA256、每帳號獨立隨機 salt、10 萬次迭代。
<b>資料庫內不存明文密碼</b>，即使資料庫外流也無法直接還原密碼。</td></tr>
<tr><td>密碼比對</td><td>以定時比較（<code>hmac.compare_digest</code>）避免時間差攻擊。</td></tr>
<tr><td>登入憑據</td><td>登入後發給隨機 session token（存於瀏覽器 cookie）。</td></tr>
<tr><td>權限檢查</td><td>所有 <code>/api</code> 請求先驗證登入，再依角色檢查。畫面隱藏按鈕僅為輔助。</td></tr>
<tr><td>資料落地</td><td>憑證影像、辨識結果、科目表皆存於本機，不上傳雲端。</td></tr>
</table>
<div class="callout warn"><div class="ct">⚠️ 現況限制（請留意）</div>
<ul>
<li><b>登入階段存於記憶體</b>：伺服器重新啟動後所有人需重新登入。此為刻意設計（重啟即清除所有階段），但也表示無法「強制某一使用者登出」而不影響他人。</li>
<li><b>系統未強制密碼定期更換、未鎖定連續登入失敗</b>。若貴公司資安政策有此要求，屬設計變更項目。</li>
<li><b>目前無網頁版帳號管理畫面</b>，帳號維護需由管理員在伺服器端以工具執行（見下節）。</li>
<li>契約載明本次開發具備帳號密碼登入等基本資安防護；<b>第三方弱點掃描與滲透測試不在範圍內</b>。</li>
</ul></div>
</section>

<section class="doc"><h2><span class="ic">🛠️</span>帳號維護作業</h2>
<p class="lead">於伺服器主機開啟命令提示字元，切換至系統資料夾後執行。
所有密碼皆以互動方式輸入，不會顯示於畫面、也不會留在命令歷史紀錄中。</p>

<h3>檢視所有帳號（含是否仍為預設密碼）</h3>
<div class="codeblock"><span class="lang">指令</span><pre>python scripts/manage_users.py list</pre></div>
<div class="codeblock"><span class="lang">輸出範例</span><pre>帳號              角色          顯示名稱            密碼狀態
admin           管理員         系統管理員           ⚠ 仍為預設密碼
clerk           記帳士         記帳士             已變更
accountant      會計師         會計師             已變更</pre></div>

<h3>變更密碼（上線前三個帳號都要做）</h3>
<div class="codeblock"><span class="lang">指令</span><pre>python scripts/manage_users.py passwd admin</pre></div>
<p>系統會要求輸入兩次新密碼。密碼至少 8 字元，且不接受常見或預設密碼。</p>

<h3>新增帳號</h3>
<div class="codeblock"><span class="lang">指令</span><pre>python scripts/manage_users.py add wangms clerk 王小姐</pre></div>
<p>格式為：<code>add &lt;帳號&gt; &lt;角色&gt; &lt;顯示名稱&gt;</code>，角色須為
<code>clerk</code>（記帳士）、<code>accountant</code>（會計師）或 <code>admin</code>（管理員）。</p>

<h3>更改顯示名稱／刪除帳號</h3>
<div class="codeblock"><span class="lang">指令</span><pre>python scripts/manage_users.py rename wangms 王大明
python scripts/manage_users.py delete wangms</pre></div>
<p>刪除需輸入 <code>yes</code> 二次確認。系統會阻止刪除<b>最後一個管理員帳號</b>，避免無人可管理。</p>

<div class="callout tip"><div class="ct">🛠️ 建議的帳號規劃</div>
<ul>
<li>實際使用者<b>各自使用專屬帳號</b>，不要共用；系統的修正紀錄以帳號區分責任。</li>
<li><code>admin</code> 帳號僅供系統維護（改設定、備份、管理帳號），日常記帳請用個人帳號。</li>
<li>人員離職時請以 <code>delete</code> 移除帳號，或先 <code>passwd</code> 變更密碼再移除。</li>
</ul></div>
</section>

<section class="doc"><h2><span class="ic">💾</span>備份與還原</h2>
<p class="lead">備份為<b>管理員專屬</b>功能，於系統畫面右上角「💾 備份」執行。</p>
<table>
<tr><th>項目</th><th>說明</th></tr>
<tr><td>備份內容</td><td>資料庫（憑證、辨識結果、審核紀錄、帳號）與設定檔（會計科目表、公司登錄、模型設定），打包為 zip。</td></tr>
<tr><td>備份位置</td><td>本機系統資料夾內，可從畫面下載後另存至貴公司指定的儲存媒體。</td></tr>
<tr><td>建議頻率</td><td>每月結帳完成後至少一次；重大調整（如大量匯入、科目表變更）前後各一次。</td></tr>
<tr><td>還原方式</td><td>將 zip 內容還原至系統資料夾後重新啟動。<b>還原會覆蓋現有資料，執行前請先備份當下狀態。</b></td></tr>
</table>
<div class="callout warn"><div class="ct">⚠️ 備份檔含機密資料</div>
備份檔內含完整憑證影像與財務資料，以及帳號密碼雜湊值。請比照財務文件保管，
勿存放於未加密的共用空間。</div>
</section>

<section class="doc"><h2><span class="ic">📄</span>文件資訊</h2>
<table>
<tr><th>項目</th><th>內容</th></tr>
<tr><td>文件名稱</td><td>管理者帳號權限說明文件</td></tr>
<tr><td>對應契約</td><td>委託技術服務契約書　附件一計畫書　五、交付項目：
「OCR 自動辨識分類正式版模組：包含管理者帳號權限文件一份」</td></tr>
<tr><td>適用版本</td><td>依產生日期之系統版本；系統權限調整後請重新產生本文件</td></tr>
<tr><td>產生方式</td><td>{date.today().isoformat()}　由 <code>scripts/build_admin_doc.py</code> 依程式碼實際行為產生</td></tr>
</table>
</section>
</main></body></html>"""


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML, encoding="utf-8")
    print(f"→ {OUT.relative_to(ROOT)}　{OUT.stat().st_size/1024:.0f} KB")


if __name__ == "__main__":
    main()
