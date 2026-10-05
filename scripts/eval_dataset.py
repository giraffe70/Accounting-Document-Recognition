"""測試集比對計分：憑證跑完整管線，與客戶傳票的實際分錄比對。

流程（每案）：
  傳票頁 → journal_voucher 辨識 → **標準答案分錄**
  憑證頁 → Router 判類型 → 專屬 agent 萃取 → classify 分類 → **系統分錄**
  兩者比對計分

比對方式刻意用「**科目名稱**」而非代號：客戶傳票用的是仲彬 ERP 代號（6205 運費），
系統依事務所科目表產出（6050 運費），代號永遠對不上、比了沒有意義。
另外一張傳票常對應多張憑證（加油 3 張發票加總為一筆分錄），故系統端的分錄
先跨頁彙總後再比，並以 (借貸方向, 科目名稱) 為鍵加總金額，避免拆分粒度不同造成誤判。

結果快取於 docs/dev-notes/eval-result.json，重跑時已完成的案例直接沿用（--force 重算）。

用法：
    python scripts/eval_dataset.py            # 跑測試組
    python scripts/eval_dataset.py --all      # 訓練組也跑（僅供對照，數字不可用於驗收）
    python scripts/eval_dataset.py --force    # 忽略快取重算
"""
import json
import pathlib
import re
import sys
import time
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs/dev-notes/dataset-manifest.json"
RESULT = ROOT / "docs/dev-notes/eval-result.json"
REPORT = ROOT / "docs/dev-notes/測試集比對結果.md"
SRC = ROOT / "ref/TEST_DATA"
WORK = ROOT / "app/data/uploads/_eval"
COMPANY = "仲彬"        # 參考資料的傳票抬頭均為仲彬股份有限公司


# 兩套科目表的等價對映（客戶仲彬 ERP 名稱 → 事務所實業表名稱）。
# **這是暫定假設，尚未與客戶確認**——列在此處而非埋在程式裡，是為了讓人一眼看出
# 哪些「不一致」其實是科目表命名差異、哪些才是分類真的選錯。
EQUIV = {
    # 仲彬把銀行帳戶細分到每一家（花旗乙存／台銀乙存…），實業表只有一個「銀行存款」
    **{f"{b}乙存": "銀行存款" for b in ("花旗", "台銀", "新光", "國泰", "合庫", "中小", "華泰", "富邦")},
    **{f"{b}支存": "銀行存款" for b in ("花旗", "台銀", "國泰", "合庫", "中小")},
    "水電費": "水電瓦斯費",      # 實業 6090
    "租金費用": "租金支出",       # 實業 6020
    "電話費": "郵電費",          # 實業 6060
    "代收款": "代收款項",         # 實業 2990
}


def norm_name(s: str) -> str:
    """科目名稱正規化：去空白/括號註記，並套等價對映，供跨科目表比對。"""
    s = re.sub(r"[\s（）()]", "", str(s or ""))
    s = re.sub(r"-\d+$", "", s)          # 「花旗乙存-018」→「花旗乙存」
    return EQUIV.get(s, s)


def agg(entries) -> dict:
    """分錄彙總為 {(借|貸, 科目名稱): 金額}，避免拆分粒度不同造成誤判。"""
    out = {}
    for e in entries:
        d = e.get("debit_account_name") or ""
        c = e.get("credit_account_name") or ""
        amt = float(e.get("amount") or 0)
        if not amt:
            continue
        key = ("借", norm_name(d)) if d else (("貸", norm_name(c)) if c else None)
        if key and key[1]:
            out[key] = round(out.get(key, 0) + amt, 2)
    return out


def render(pdf: pathlib.Path, page: int) -> str:
    import pypdfium2 as pdfium

    WORK.mkdir(parents=True, exist_ok=True)
    out = WORK / f"{pdf.stem[:24]}_p{page}.png"
    if not out.exists():
        pdfium.PdfDocument(str(pdf))[page].render(scale=2.2).to_pil().save(out)
    return str(out)


def run_case(case: dict) -> dict:
    from app.core import pipeline
    from app.models.schemas import Voucher
    from app.services import classify

    pdf = SRC / case["file"]
    # ① 標準答案：傳票頁
    expected, exp_raw, kinds = {}, [], []
    for p in case["answer_pages"]:
        v = pipeline.extract_for_type(render(pdf, p), "journal_voucher")
        exp_raw += [e.model_dump() for e in v.entries]
        kinds.append(str(v.fields.get("voucher_kind", "")))
    expected = agg(exp_raw)
    # 答案品質閘門：標準答案來自我們自己的傳票辨識，不可盡信。轉帳傳票必然借貸平衡，
    # 不平衡即為辨識錯誤；現支／收入傳票只印一面（另一面為現金），單面才是正確的。
    a_dr = sum(a for (s, _), a in expected.items() if s == "借")
    a_cr = sum(a for (s, _), a in expected.items() if s == "貸")
    one_sided = any(k in " ".join(kinds) for k in ("現支", "收入", "支出"))
    answer_reliable = bool(expected) and (
        abs(a_dr - a_cr) < 1 or (one_sided and (a_dr == 0) != (a_cr == 0)))

    # ② 系統輸出：憑證頁走完整管線（Router 判類型 → 萃取 → 分類）
    #
    # 同一案的多頁憑證有兩種情況，處理方式不同：
    #   (a) 多筆交易加總（加油 3 張發票、計程車 2 張乘車證明）→ 各自分錄後相加
    #   (b) 同一筆交易的多份證明（中華電信：電子發票＋繳費通知＋繳費結果通知）
    #       → 只能算一次，否則金額會翻倍
    # 以「總額相同」判定 (b)，對應系統既有的「合併為一張（主頁＋附頁，只辨識主頁）」。
    pred_raw, types, seen_totals = [], [], set()
    skipped = []
    for p in case["input_pages"]:
        img = render(pdf, p)
        tk = pipeline.prepare_image(img)          # Router + 自動轉正
        v = pipeline.extract_for_type(img, tk)
        tot = v.total_amount
        if tot and tot in seen_totals:            # 同一筆交易的另一份證明
            skipped.append(p)
            continue
        if tot:
            seen_totals.add(tot)
        types.append(tk)
        v = classify.classify(v, company=COMPANY)
        pred_raw += [e.model_dump() for e in v.entries]
    predicted = agg(pred_raw)

    exp_dr = round(sum(a for (s, _), a in expected.items() if s == "借"), 2)
    pred_dr = round(sum(a for (s, _), a in predicted.items() if s == "借"), 2)
    names_exp = {(s, n) for (s, n) in expected}
    names_pred = {(s, n) for (s, n) in predicted}
    # 金額結構：忽略科目名稱，只看「借貸各是哪些金額」。兩套科目表名稱本來就不同
    # （客戶 租金費用／系統 租金支出），混在一起比會分不出是萃取錯還是分類選了別的科目。
    struct_exp = sorted((s, a) for (s, _), a in expected.items())
    struct_pred = sorted((s, a) for (s, _), a in predicted.items())
    return {
        "file": case["file"], "split": case["split"], "doc_types": types,
        "merged_pages": skipped,      # 因總額重複而視為同一交易、未重複計算的頁
        "voucher_kind": "/".join(k for k in kinds if k),
        "answer_reliable": answer_reliable,
        "expected": {f"{s}|{n}": a for (s, n), a in expected.items()},
        "predicted": {f"{s}|{n}": a for (s, n), a in predicted.items()},
        "total_expected": exp_dr, "total_predicted": pred_dr,
        "total_match": abs(exp_dr - pred_dr) < 1,
        "structure_match": struct_exp == struct_pred,
        "accounts_match": names_exp == names_pred,
        "amounts_match": expected == predicted,
        "missing": sorted(f"{s}|{n}" for (s, n) in names_exp - names_pred),
        "extra": sorted(f"{s}|{n}" for (s, n) in names_pred - names_exp),
    }


def _repeat_arg() -> int:
    for a in sys.argv:
        if a.startswith("--repeat="):
            return max(1, int(a.split("=", 1)[1]))
    return 1


def run_repeated(case: dict, n: int) -> dict:
    """同一案跑 n 次，回報「每次都過」與「至少一次過」。

    地端模型 run-to-run 變異很大（實測同一張圖有時回完整 JSON、有時不回；
    傳票的借貸方向也會飄），單次結果不足以判斷改動有沒有效。跑多次才分得出
    「真的修好了」與「這次剛好對」。
    """
    runs = []
    for _ in range(n):
        try:
            runs.append(run_case(case))
        except Exception as exc:  # noqa: BLE001
            runs.append({"file": case["file"], "split": case["split"], "error": str(exc)})
    base = next((r for r in runs if not r.get("error")), runs[0])
    keys = ("total_match", "structure_match", "accounts_match", "amounts_match", "answer_reliable")
    base = dict(base)
    base["runs"] = n
    for k in keys:
        hits = sum(1 for r in runs if r.get(k))
        base[k] = hits == n                    # 全部通過才算通過（保守）
        base[f"{k}_hits"] = hits
    base["errors"] = sum(1 for r in runs if r.get("error"))
    base["unstable"] = any(0 < base[f"{k}_hits"] < n for k in keys) or 0 < base["errors"] < n
    return base


def main():
    force = "--force" in sys.argv
    repeat = _repeat_arg()
    splits = {"train", "test"} if "--all" in sys.argv else {"test"}
    cases = [c for c in json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
             if c["split"] in splits]
    done = {}
    if RESULT.exists() and not force:
        done = {r["file"]: r for r in json.loads(RESULT.read_text(encoding="utf-8"))}

    results = []
    for i, c in enumerate(cases, 1):
        if c["file"] in done:
            results.append(done[c["file"]])
            print(f"[{i}/{len(cases)}] 快取 {c['file']}")
            continue
        t = time.time()
        try:
            r = run_repeated(c, repeat) if repeat > 1 else run_case(c)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            r = {"file": c["file"], "split": c["split"], "error": str(exc)}
        r["secs"] = round(time.time() - t, 1)
        results.append(r)
        RESULT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        ok = "金額全對" if r.get("amounts_match") else (
            "科目全對" if r.get("accounts_match") else ("總額相符" if r.get("total_match") else "不符"))
        print(f"[{i}/{len(cases)}] {c['file'][:34]:<36} {ok}  {r['secs']}s")

    write_report(results)


def write_report(results):
    # 只以「答案可信」的案例計分——標準答案本身來自傳票辨識，錯的答案不能拿來評分
    scored = [r for r in results if r.get("answer_reliable")]
    unreliable = [r for r in results if not r.get("error") and not r.get("answer_reliable")]
    failed = [r for r in results if r.get("error")]
    n = len(scored)
    ok_amt = sum(1 for r in scored if r.get("amounts_match"))
    ok_acc = sum(1 for r in scored if r.get("accounts_match"))
    ok_str = sum(1 for r in scored if r.get("structure_match"))
    ok_tot = sum(1 for r in scored if r.get("total_match"))
    pct = (lambda x: f"{x / n:.0%}") if n else (lambda x: "—")
    L = ["# 測試集比對結果", "",
         "> 由 `scripts/eval_dataset.py` 產生，勿手改。憑證頁跑完整管線（Router→萃取→分類），",
         "> 與客戶傳票辨識出的實際分錄比對。", "",
         "## 指標定義", "",
         "客戶傳票用**仲彬 ERP 科目代號**、系統依**事務所科目表**產出，代號本來就不同，",
         "故一律以科目**名稱**比對；且兩表連名稱都可能不同（客戶「租金費用」／系統「租金支出」、",
         "客戶「應付費用」／系統「應付帳款」），所以另外獨立量測「金額結構」：", "",
         "| 指標 | 意義 | 主要反映 |",
         "|---|---|---|",
         "| 借方總額相符 | 借方金額合計一致 | 欄位萃取的金額對不對 |",
         "| 金額結構相符 | 借貸各筆金額完全一致（不看科目名稱）| 萃取＋稅額拆分是否正確 |",
         "| 科目相符 | 借貸方向＋科目名稱集合一致 | 分類選的科目是否與客戶相同 |",
         "| 科目與金額全對 | 上述兩者同時成立 | 端到端完全一致 |", "",
         "**多頁憑證的處理**：同一案的多頁有兩種情況——多筆交易加總（加油 3 張發票）",
         "與同一筆交易的多份證明（中華電信：電子發票＋繳費通知＋繳費結果通知）。",
         "後者若各自產生分錄再相加，金額會翻倍，故以「總額相同」判定為同一交易只計一次，",
         "對應系統既有的「合併為一張（主頁＋附頁，只辨識主頁）」。逐案表的「合併頁」欄即為被略過的頁。", "",
         "## 答案品質閘門", "",
         "標準答案來自**我們自己的傳票辨識**，不是人工核對過的，錯的答案不能拿來評分。",
         "轉帳傳票依定義必然借貸平衡，不平衡即為辨識錯誤；現支／收入傳票只印一面",
         "（另一面為現金，隱含於傳票類型），單面才是正確的。據此篩選：", "",
         f"- 可計分：**{len(scored)}** 案",
         f"- 答案不可信（傳票辨識有誤，待人工核對）：{len(unreliable)} 案"
         + (f"　{'、'.join(r['file'][:16] for r in unreliable)}" if unreliable else ""),
         f"- 執行失敗：{len(failed)} 案"
         + (f"　{'、'.join(r['file'][:16] for r in failed)}" if failed else ""), "",
         *( [] if not any(r.get("runs") for r in results) else [
             "## 穩定性", "",
             f"每案重複執行 **{max((r.get('runs') or 1) for r in results)} 次**，"
             "指標須**每次都通過**才算通過（保守計法）。",
             f"結果會變動的案例：**{sum(1 for r in results if r.get('unstable'))}** / {len(results)}"
             "　←　這個數字若高，代表單次評估的數字不可用於判斷改動成效。", ""]),
         f"## 總計（{n} 案可計分）", "",
         "| 指標 | 通過 | 比例 |", "|---|---|---|",
         f"| 借方總額相符 | {ok_tot} / {n} | {pct(ok_tot)} |",
         f"| 金額結構相符 | {ok_str} / {n} | {pct(ok_str)} |",
         f"| 科目相符 | {ok_acc} / {n} | {pct(ok_acc)} |",
         f"| 科目與金額全對 | {ok_amt} / {n} | {pct(ok_amt)} |", "",
         "## 逐案", "",
         "| 案例 | 傳票別 | 判定類型 | 合併頁 | 總額(答案/系統) | 結構 | 科目 | 全對 |",
         "|---|---|---|---|---|---|---|---|"]
    m = lambda b: "✓" if b else "✗"
    for r in results:
        if r.get("error"):
            L.append(f"| {r['file']} | — | — | — | 執行失敗：{r['error'][:40]} | — | — | — |")
            continue
        mark = "" if r.get("answer_reliable") else "⚠️答案不可信 "
        L.append(f"| {mark}{r['file']} | {r.get('voucher_kind', '')} | {'/'.join(r['doc_types'])} | "
                 f"{'p' + ',p'.join(map(str, r.get('merged_pages') or [])) if r.get('merged_pages') else '—'} | "
                 f"{r['total_expected']:,.0f} / {r['total_predicted']:,.0f} | "
                 f"{m(r['structure_match'])} | {m(r['accounts_match'])} | {m(r['amounts_match'])} |")
    L += ["", "## 科目差異明細", "",
          "系統選的科目與客戶不同之處（判斷是否為等價科目需會計專業，此處僅列出）：", ""]
    for r in scored:
        if r.get("accounts_match"):
            continue
        L.append(f"- **{r['file']}**")
        L.append(f"  - 客戶：{'、'.join(f'{k} {v:,.0f}' for k, v in r['expected'].items())}")
        L.append(f"  - 系統：{'、'.join(f'{k} {v:,.0f}' for k, v in r['predicted'].items())}")
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\n總額 {ok_tot}/{n}、科目 {ok_acc}/{n}、金額 {ok_amt}/{n} → {REPORT.name}")


if __name__ == "__main__":
    main()
