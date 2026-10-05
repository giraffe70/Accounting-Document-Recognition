"""用訓練組的客戶傳票產生分類 few-shot 種子。

分類的 RAG 原本只從「已核定」紀錄取範例，系統剛上線時一筆都沒有，等於 zero-shot——
實測科目選得很籠統（有「運費」卻用「其他費用」）。客戶提供的傳票是現成的正解，
拿訓練組的幾案當種子可直接補上這個缺口。

**只寫科目名稱不寫代號**：傳票用的是客戶 ERP 代號（6205 運費），系統依事務所科目表
產出（6050 運費），把代號餵下去會教模型吐錯的代號。名稱由模型再對照科目表取代號。

用法：python scripts/seed_examples.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs/dev-notes/dataset-manifest.json"
OUT = ROOT / "app/data/entry_examples.json"
SRC = ROOT / "ref/TEST_DATA"


def main():
    import pypdfium2 as pdfium

    from app.core import pipeline

    cases = [c for c in json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
             if c["split"] == "train"]
    seeds = []
    for c in cases:
        pdf = SRC / c["file"]
        for p in c["answer_pages"]:
            tmp = ROOT / "app/data/uploads/_eval" / f"seed_{pdf.stem[:20]}_p{p}.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            pdfium.PdfDocument(str(pdf))[p].render(scale=2.2).to_pil().save(tmp)
            v = pipeline.extract_for_type(str(tmp), "journal_voucher")
            entries = []
            for e in v.entries:
                name = e.debit_account_name or e.credit_account_name
                if not name or not e.amount:
                    continue
                entries.append({"科目": name, "金額": e.amount,
                                "借貸": "借" if e.debit_account_name else "貸"})
            if entries:
                seeds.append({
                    "來源": c["file"],
                    "傳票別": v.fields.get("voucher_kind", ""),
                    "摘要": (v.entries[0].description or "")[:40] if v.entries else "",
                    "分錄": entries,
                })
                print(f"  {c['file'][:30]:<32} {len(entries)} 筆分錄")
    OUT.write_text(json.dumps(seeds, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n寫入 {OUT.relative_to(ROOT)}：{len(seeds)} 案")


if __name__ == "__main__":
    main()
