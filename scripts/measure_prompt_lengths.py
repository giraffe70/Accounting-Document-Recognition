"""Offline, reproducible prompt size comparison against the frozen pre-refactor baseline."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core import doc_types, prompts


def measure():
    baseline_path = ROOT / "docs/dev-notes/prompt-length-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    rows = []
    for key in doc_types.DOC_TYPES:
        row = {"type_key": key}
        for mode in ("image", "ocr_text"):
            text = prompts.build_extract_prompt(key, mode=mode, ocr_text=baseline["evidence"])
            row[mode] = {"before_characters": baseline["characters"][key][mode], "after_characters": len(text),
                         "after_utf8_bytes": len(text.encode("utf-8"))}
        rows.append(row)
    averages = {mode: {"before": sum(r[mode]["before_characters"] for r in rows)/len(rows),
                       "after": sum(r[mode]["after_characters"] for r in rows)/len(rows)} for mode in ("image", "ocr_text")}
    report = {"baseline_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
              "evidence": baseline["evidence"], "unit": "Unicode characters, not model tokens",
              "rows": rows, "averages": averages, "core_template_count": 3, "document_contract_count": len(rows)}
    output = ROOT / "docs/dev-notes/prompt-length-report"
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    lines = ["# Prompt 精簡離線比較", "", "相同合成 OCR 證據，比較生成器的完整文字輸入：核心指令、契約、語意提示及 OCR 全部計入。圖片 bytes 不計字元，未加入公司資料或動態案例。新契約含付款資訊與扣繳欄位；不以刪除欄位縮短输入。", "",
             "計量單位為 Unicode 字元；不是模型 token、速度或準確率。基準不含私有影像或 gold。", "",
             "| 模式 | 改版前平均 | 改版後平均 | 減少 |", "|---|---:|---:|---:|"]
    for mode, values in averages.items():
        lines.append(f"| {mode} | {values['before']:.1f} | {values['after']:.1f} | {100*(1-values['after']/values['before']):.1f}% |")
    lines += ["", "| 類型 | 圖片：前 → 後 | 純文字：前 → 後 |", "|---|---:|---:|"]
    lines += [f"| {r['type_key']} | {r['image']['before_characters']} → {r['image']['after_characters']} | {r['ocr_text']['before_characters']} → {r['ocr_text']['after_characters']} |" for r in rows]
    lines += ["", "擷取平均輸入縮短；部分欄位繁多的契約可能增加，明細與缺值定義仍須完整。會計分類加入完整證據與公司政策，輸入長度依當次資料而變，不以舊摘要少傳資料作為比較基準。", "",
              "## 單任務呼叫上限", "", "| 任務 | 改版前 | 改版後 |", "|---|---:|---:|",
              "| 分類 Router | 3 | 6 |", "| 欄位擷取 | 9 | 6 |", "| 選配補讀 | 3 | 6 |", "| 會計分類 | 1 | 6 |", "",
              "新版每任務初次＋一次契約重試，每輪最多三次傳輸重試；正常回應每任務仍只呼叫一次。補讀只於既有缺統編條件觸發；含補讀的擷取最壞上限維持 12 次。重試是完整原證據重送，不以截斷 JSON 猜修。", "",
              "重新產出：`python scripts/measure_prompt_lengths.py`（使用 pdfscan312，無模型呼叫）。真實模型的正確率、漏填率、人工修改量、實際重試次數與耗時，仍需固定資料集比較。", ""]
    output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return report


if __name__ == "__main__":
    for mode, values in measure()["averages"].items():
        print(f"{mode}: {values['before']:.1f} → {values['after']:.1f} characters")
