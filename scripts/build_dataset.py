"""整理客戶提供的參考單據為「訓練組／測試組」，產出 manifest 與開發筆記。

本專案沒有模型微調，所謂「訓練組」指的是：**用來訂提示詞規則、或餵給 classify
的 bge-m3 RAG 當 few-shot 範例**的案例；「測試組」則是完全不參與上述用途、
只用來量測準確率的保留案例。兩者混用會讓數字失真（自己考自己），故明確切分。

判定方式：逐頁 OCR 抬頭，含「轉帳傳票／傳票單號／傳票單別」者為傳票（＝標準答案），
其餘為憑證（＝系統的輸入）。傳票上有客戶實際使用的會計科目與借貸金額。

用法：
    python scripts/build_dataset.py            # 逐頁 OCR 重新判定（約 2-3 分鐘）
    python scripts/build_dataset.py --cache    # 沿用 manifest 既有的頁面判定
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "ref/TEST_DATA"
MANIFEST = ROOT / "docs/dev-notes/dataset-manifest.json"
NOTE = ROOT / "docs/dev-notes/測試資料集.md"

# 切分（N=17，故以「同型是否已有代表」為原則手動指定，並記錄理由）
TRAIN = {
    "3.進口關稅-傳票+計算式+進口報單.pdf":
        "已用於改寫 IMPORT_CLASSIFY_PROMPT（進口稅費掛預付費用），答案已洩漏，不可用於評估",
}
# 2026-07-26 使用者裁示：測試組要最大化，只保留已污染的案例在訓練組。
# 原先另有兩案（1.運費、11.加油油資）作 few-shot 種子，已移回測試組——
# 代價是種子只剩進口關稅一案（全是進口相關分錄），一般費用類的科目選擇失去範例，
# 科目正確率預期會下降。正解是請客戶另外提供專供種子用的傳票，見筆記末段。


def detect(path: pathlib.Path) -> list:
    import pypdfium2 as pdfium

    from app.core.ocr_engine import PaddleOCREngine

    ocr = PaddleOCREngine()
    tmp = ROOT / "app/data/uploads/_pages/_dsprobe.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(path))
    kinds = []
    for i in range(len(doc)):
        im = doc[i].render(scale=1.8).to_pil()
        im.crop((0, 0, im.width, int(im.height * 0.22))).save(tmp)
        t = "".join(l["text"] for l in ocr.recognize(str(tmp)))
        kinds.append("傳票" if any(k in t for k in ("轉帳傳票", "傳票單號", "傳票單別")) else "憑證")
    return kinds


def main():
    use_cache = "--cache" in sys.argv
    old = {}
    if MANIFEST.exists():
        old = {c["file"]: c for c in json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]}

    cases = []
    for f in sorted(SRC.glob("*.pdf")):
        if "會計科目" in f.name:
            continue
        if use_cache and f.name in old:
            kinds = old[f.name]["pages"]
        else:
            kinds = detect(f)
        answer_pages = [i for i, k in enumerate(kinds) if k == "傳票"]
        input_pages = [i for i, k in enumerate(kinds) if k == "憑證"]
        has_answer = bool(answer_pages)
        cases.append({
            "file": f.name,
            "pages": kinds,
            "answer_pages": answer_pages,     # 傳票＝標準答案
            "input_pages": input_pages,       # 憑證＝系統輸入
            "split": ("train" if f.name in TRAIN else "test") if has_answer else "unlabeled",
            "reason": TRAIN.get(f.name, ""),
        })

    data = {
        "note": "傳票＝標準答案（客戶實際分錄），憑證＝系統輸入。unlabeled 為無傳票、"
                "僅供辨識調校的樣本，不列入準確率計算。",
        "counts": {
            "train": sum(1 for c in cases if c["split"] == "train"),
            "test": sum(1 for c in cases if c["split"] == "test"),
            "unlabeled": sum(1 for c in cases if c["split"] == "unlabeled"),
        },
        "cases": cases,
    }
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    tr = [c for c in cases if c["split"] == "train"]
    te = [c for c in cases if c["split"] == "test"]
    un = [c for c in cases if c["split"] == "unlabeled"]
    L = []
    A = L.append
    A("# 測試資料集：訓練組／測試組")
    A("")
    A("> 由 `scripts/build_dataset.py` 產生，勿手改表格；切分規則寫在該腳本的 `TRAIN` 常數。")
    A("> 對應清單：`docs/dev-notes/dataset-manifest.json`（機器可讀）。")
    A("")
    A("## 這裡的「訓練」是什麼意思")
    A("")
    A("本專案**沒有模型微調**，地端 VLM／LLM 都是直接使用。所以：")
    A("")
    A("- **訓練組** ＝ 用來**訂提示詞規則**、或餵給 `classify` 的 bge-m3 RAG 當 few-shot 範例的案例。")
    A("- **測試組** ＝ 完全不參與上述用途、只用來**量測準確率**的保留案例。")
    A("")
    A("兩者若混用，等於拿看過的答案考自己，數字會虛高。故明確切分並記錄每個案例的歸屬理由。")
    A("")
    A("## 資料結構")
    A("")
    A("客戶提供的每個 PDF 內含 **1 頁轉帳傳票（客戶實際分錄＝標準答案）＋ 1~4 頁原始憑證（系統輸入）**。")
    A("傳票上有會計科目代號／名稱、借貸金額、摘要與立沖帳目（廠商代號）。")
    A("")
    A(f"## 訓練組（{len(tr)} 案）")
    A("")
    A("| 案例 | 頁面組成 | 列入訓練組的理由 |")
    A("|---|---|---|")
    for c in tr:
        A(f"| {c['file']} | {len(c['answer_pages'])} 傳票 + {len(c['input_pages'])} 憑證 | {c['reason']} |")
    A("")
    A(f"## 測試組（{len(te)} 案）— 未參與規則制定，可用於驗收")
    A("")
    A("| 案例 | 頁面組成 |")
    A("|---|---|")
    for c in te:
        A(f"| {c['file']} | {len(c['answer_pages'])} 傳票 + {len(c['input_pages'])} 憑證 |")
    A("")
    A(f"## 無答案樣本（{len(un)} 案）")
    A("")
    A("只有憑證、沒有對應傳票，**不列入準確率計算**；供辨識欄位調校與版型覆蓋度檢查。")
    A("")
    A("| 案例 | 頁數 |")
    A("|---|---|")
    for c in un:
        A(f"| {c['file']} | {len(c['pages'])} |")
    A("")
    A("## 種子資料不足（待客戶提供）")
    A("")
    A("分類的 few-shot 種子必須來自**測試組以外**的資料，否則等於拿看過的答案考自己。")
    A("目前唯一可用的是進口關稅一案（全為進口相關分錄），一般費用類（運費、租金、")
    A("保險費、水電、勞健保…）沒有任何範例，科目選擇只能靠提示詞規則。")
    A("")
    A("**建議請客戶另外提供 10~20 組傳票＋憑證專供種子用**（不列入測試），")
    A("涵蓋日常各類費用即可，都是現成的既有帳務資料，客戶成本很低。")
    A("系統上線後累積的「已核定」紀錄也會自動進入 RAG，屆時可取代種子。")
    A("")
    A("## 誠實揭露：提示詞規則的來源")
    A("")
    A("除錯過程中曾檢視測試組傳票的答案（為了找出金額為何兜不攏、科目為何選錯），")
    A("據此寫入的規則（如「貸方依是否已付款區分應付費用／應付帳款」）雖屬台灣會計慣例、")
    A("非本測試集獨有，但仍使目前數字**略為樂觀**。真正乾淨的驗收數字需要一組")
    A("**從未被檢視過**的新資料。")
    A("")
    A("## 尚未完成")
    A("")
    A("此檔只完成**資料切分**。要得出準確率還需要：")
    A("")
    A("1. **傳票辨識**：新增 `journal_voucher` 類型，把傳票頁讀成結構化分錄"
      "（電腦列印、版面固定，準確度應該很高）。")
    A("2. **比對計分**：憑證頁 → 現有管線辨識＋分類 → 與傳票分錄比對，"
      "逐案記錄科目是否正確、金額是否正確。")
    A("3. 產出如「測試組 14 案中，科目全對 N 案、金額全對 M 案」的驗收數字。")

    NOTE.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"train {len(tr)} / test {len(te)} / unlabeled {len(un)}")
    print("written:", MANIFEST.name, NOTE.name)


if __name__ == "__main__":
    main()
