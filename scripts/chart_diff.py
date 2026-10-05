"""比對「仲彬 ERP 科目表」與「實業事務所科目表」，產出開發筆記。

比對以**科目名稱**為鍵（代號兩邊本來就不同，用代號比毫無意義），
名稱先正規化（去空白、全形括號轉半形、簡轉繁）再比。
"""
import pathlib
import re
import sys
import unicodedata

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import openpyxl

from app.core.pipeline import to_traditional
from app.services import accounts

ROOT = pathlib.Path(__file__).resolve().parent.parent

CAT = {"1": "資產", "2": "負債", "3": "業主權益", "4": "營業收入",
       "5": "營業成本", "6": "營業費用", "7": "營業外收支", "8": "所得稅"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKC", to_traditional(str(name).strip()))
    return re.sub(r"[\s（）()\-－]", "", s)


# 仲彬（客戶 ERP）
wb = openpyxl.load_workbook(r"C:\Dev\PDF_SCAN_Recongnition\ref\TEST_DATA\仲彬科目代號表.xlsx",
                            data_only=True)
zb = {}
for r in wb.worksheets[0].iter_rows(values_only=True):
    if r[0] in (None, "") or r[1] in (None, ""):
        continue
    c, n = str(r[0]).strip(), to_traditional(str(r[1]).strip())
    if c.isdigit():
        zb[c] = n

# 實業（事務所，目前系統採用）
sy = {a["code"]: a["name"] for a in accounts.load_accounts()}

zb_by_name, sy_by_name = {}, {}
for c, n in zb.items():
    zb_by_name.setdefault(norm(n), []).append((c, n))
for c, n in sy.items():
    sy_by_name.setdefault(norm(n), []).append((c, n))

same_name = sorted(set(zb_by_name) & set(sy_by_name))
only_sy = sorted(set(sy_by_name) - set(zb_by_name))
only_zb = sorted(set(zb_by_name) - set(sy_by_name))

lines = []
A = lines.append
A("# 會計科目：兩版本差異總表")
A("")
A("> 產生方式：`ref/TEST_DATA/仲彬科目代號表.xlsx`（265 個）對比 "
  "`ref/TEST_DATA/實業會計科目.pdf`（39 個，OCR 解析）。")
A("> 以**科目名稱**比對（代號兩套本來就不同），名稱經去空白／全半形／簡繁正規化。")
A("> 由程式產生，勿手改；來源檔更新後重跑比對腳本。")
A("")
A("## 背景")
A("")
A("- **實業** ＝ 會計師事務所，其科目表為**三家公司（仲彬／中意／天干）共用標準**，"
  "本系統 `app/data/accounts.json` 的通用表即採用此版本。")
A("- **仲彬** ＝ 客戶自有 ERP 的科目表，客戶**實際開立的轉帳傳票用的是這一套代號**"
  "（見 `ref/TEST_DATA/*傳票*.pdf`）。")
A("- 兩套代號完全不同（例：運費 仲彬 6205 ／ 實業 6050），故日後若要把系統產出的分錄"
  "回寫客戶 ERP，需要一張對照表。**本檔即為討論用底稿，尚未與客戶定案。**")
A("")
A(f"## 一、同名不同代號（{len(same_name)} 項）— 對照表主體")
A("")
A("⚠️ **對照不是一對一**：仲彬 ERP 常把同一個科目名稱依性質拆成多個代號"
  "（如「運費」有 5212／5505／5801／6205、「保險費」有 5215／5510／6210，"
  "分屬銷貨成本、營業費用等不同段），實業表則只有一個。"
  "**由實業碼反推仲彬碼時會有歧義，需客戶指定預設值。**")
A("")
A("| 科目名稱 | 仲彬 ERP | 實業事務所 |")
A("|---|---|---|")
for k in same_name:
    zc = "、".join(c for c, _ in zb_by_name[k])
    sc = "、".join(c for c, _ in sy_by_name[k])
    nm = sy_by_name[k][0][1]
    mark = "" if zc != sc else "（代號相同）"
    A(f"| {nm} | {zc} | {sc} {mark}|".replace(" |", " |"))
A("")
A(f"## 二、只有實業事務所有（{len(only_sy)} 項）")
A("")
A("仲彬 ERP 無同名科目，記帳時需確認客戶如何對應。")
A("")
A("| 代號 | 科目名稱 | 類別 |")
A("|---|---|---|")
for k in only_sy:
    c, n = sy_by_name[k][0]
    A(f"| {c} | {n} | {CAT.get(c[0], '')} |")
A("")
A(f"## 三、只有仲彬 ERP 有（{len(only_zb)} 項）— 實業表缺口")
A("")
A("**這些是客戶實際在用、但事務所標準表沒有的科目。** 系統若被要求「只用事務所科目表」，"
  "遇到這些帳務就得對映到別的科目——已知需求如下（其餘待客戶確認）：")
A("")
A("| 仲彬代號 | 科目名稱 | 目前系統對映 |")
A("|---|---|---|")
MAPPED = {"1254": "→ 1210 預付費用（2026-07-25 裁示，進口稅費用）",
          "6224": "→ 6230 其他費用（2026-07-25 裁示，銀行匯費用）"}
for k in only_zb:
    for c, n in sorted(zb_by_name[k]):
        A(f"| {c} | {n} | {MAPPED.get(c, '')} |")
A("")
A("## 待客戶討論")
A("")
A("1. 事務所科目表是否補上「預付進口款項」「手續費」？補了就不必對映。")
A("2. 系統產出的分錄最終要交給誰、用哪套代號？若要回寫客戶 ERP，本檔第一節即為對照表。")
A("3. 上表第三節數量龐大（多為銀行別、費用細分），是否全部需要對照，或只需常用者。")
A("4. 第一節的一對多（運費、保險費、文具用品、稅捐、廣告費、郵電費）由實業碼反推仲彬碼時"
  "   要用哪一個代號當預設，需客戶指定。")
A("")
A("## 產生方式")
A("")
A("此檔由比對腳本輸出。來源檔（兩份科目表）更新後請重跑，勿手動編輯表格內容；")
A("討論結論請寫在「待客戶討論」段落之後，避免下次重跑被覆蓋。")

out = str(ROOT / "docs/dev-notes/會計科目對照.md")
open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"同名不同代號 {len(same_name)}、只有實業 {len(only_sy)}、只有仲彬 {len(only_zb)}")
print("written:", out)
