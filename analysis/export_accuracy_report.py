"""Export the model accuracy comparison as PDF and Word reports."""

from __future__ import annotations

import csv
import re
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import font_manager
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


HERE = Path(__file__).resolve().parent
GOLD_CSV_PATH = HERE / "gold_standard.csv"
COMPARISON_CSV_PATH = HERE / "validation_comparison.csv"
OUTPUT_DIR = HERE / "reports"
PDF_PATH = OUTPUT_DIR / "憑證辨識模型準確率比較_CSV查核完整版.pdf"
DOCX_PATH = OUTPUT_DIR / "憑證辨識模型準確率比較_CSV查核完整版.docx"

FIELD_LABELS = {
    "doc_type_key": "單據類型",
    "date": "日期",
    "voucher_id": "憑證號",
    "total_amount": "總額",
    "subtotal": "未稅小計",
    "tax_amount": "稅額",
    "seller_name": "賣方名稱",
    "seller_tax_id": "賣方統編",
    "buyer_name": "買方名稱",
    "buyer_tax_id": "買方統編",
}
FIELDS = tuple(FIELD_LABELS)
AMOUNT_FIELDS = {"total_amount", "subtotal", "tax_amount"}
ID_FIELDS = {"voucher_id", "seller_tax_id", "buyer_tax_id"}
DOC_TYPE_LABELS = {
    "invoice": "統一發票",
    "journal_voucher": "會計傳票",
    "receipt": "收據／雜項開銷",
    "purchase_order": "採購訂單",
    "bill_detail": "費用明細",
    "cheque": "支票",
    "tax_bill": "稅款繳款書",
    "remittance": "銀行水單／匯款",
    "social_insurance": "勞健保單",
    "import_declaration": "進口報單",
    "statement": "對帳單／應收明細",
    "utility": "水電／公用事業",
    "other": "其他憑證",
}
DOC_TYPE_NOTES = {
    "invoice": "Gemma 多數落入 other",
    "journal_voucher": "Gemma 完全未能辨識傳票",
    "receipt": "包含計程車、郵資等收據",
    "purchase_order": "Qwen 識別佳",
    "bill_detail": "樣本僅 1 頁，兩模型皆未命中",
    "cheque": "Qwen 全部辨識正確",
    "remittance": "Qwen 全部辨識正確",
    "social_insurance": "勞保局、健保署相關單據",
    "import_declaration": "海關進口報單",
    "statement": "容易與發票或訂單混淆",
    "utility": "Qwen 命中 1 頁，Gemma 未命中",
    "other": "Gemma 因大量預測 other 而得高分",
}
DOC_TYPE_ORDER = (
    "invoice",
    "journal_voucher",
    "receipt",
    "purchase_order",
    "cheque",
    "remittance",
    "social_insurance",
    "import_declaration",
    "statement",
    "utility",
    "bill_detail",
    "other",
)

BLUE = "2F6FED"
NAVY = "183153"
GREEN = "248A5A"
ORANGE = "D97706"
LIGHT_BLUE = "EAF1FF"
LIGHT_GRAY = "F3F4F6"
MID_GRAY = "D1D5DB"
DARK_GRAY = "374151"
WHITE = "FFFFFF"


def pdf_color(value: str):
    return colors.HexColor(f"#{value}")


def score(correct: int, total: int) -> tuple[int, int, float]:
    return correct, total, correct / total * 100 if total else 0.0


def normalized(field: str, value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if field in AMOUNT_FIELDS:
        try:
            return str(round(float(text.replace(",", "").replace("$", "")), 2))
        except ValueError:
            return text
    if field in ID_FIELDS:
        return re.sub(r"[^A-Z0-9]", "", text.upper())
    if field == "date":
        numbers = re.findall(r"\d+", text)
        if len(numbers) >= 3:
            year, month, day = map(int, numbers[:3])
            if year < 1911:
                year += 1911
            return f"{year:04d}-{month:02d}-{day:02d}"
    return re.sub(r"\s+", "", text)


def load_report_data() -> dict:
    with GOLD_CSV_PATH.open(encoding="utf-8-sig", newline="") as stream:
        gold_rows = list(csv.DictReader(stream))
    with COMPARISON_CSV_PATH.open(encoding="utf-8-sig", newline="") as stream:
        comparison_rows = list(csv.DictReader(stream))

    gold = {(row["source"], row["page"]): row for row in gold_rows}
    comparison = {(row["source"], row["page"]): row for row in comparison_rows}
    common_keys = sorted(set(gold) & set(comparison))
    scoped_keys = [
        key for key in common_keys if gold[key]["confidence"] in ("high", "medium")
    ]

    field_counts = {
        model: {field: {"correct": 0, "total": 0} for field in FIELDS}
        for model in ("qwen", "gemma")
    }
    full_page = {model: {"correct": 0, "total": 0} for model in ("qwen", "gemma")}
    core = {model: {"correct": 0, "total": 0} for model in ("qwen", "gemma")}

    for key in scoped_keys:
        truth = gold[key]
        result = comparison[key]
        row_marks = {model: [] for model in ("qwen", "gemma")}
        for field in FIELDS:
            expected = truth[field]
            if (
                not expected.strip()
                or (truth["page_role"] == "journal_voucher" and field in ("subtotal", "tax_amount"))
            ):
                continue
            for model in ("qwen", "gemma"):
                predicted = result[f"{model}_{field}"]
                matched = normalized(field, predicted) == normalized(field, expected)
                field_counts[model][field]["total"] += 1
                field_counts[model][field]["correct"] += int(matched)
                row_marks[model].append(matched)

        for model in ("qwen", "gemma"):
            if len(row_marks[model]) >= 2:
                full_page[model]["total"] += 1
                full_page[model]["correct"] += int(all(row_marks[model]))

        core_eligible = (
            truth["page_role"] != "journal_voucher"
            and all(truth[field].strip() for field in ("date", "voucher_id", "total_amount"))
        )
        if core_eligible:
            for model in ("qwen", "gemma"):
                matched = all(
                    normalized(field, comparison[key][f"{model}_{field}"])
                    == normalized(field, truth[field])
                    for field in ("date", "voucher_id", "total_amount")
                )
                core[model]["total"] += 1
                core[model]["correct"] += int(matched)

    fields = []
    for key, label in FIELD_LABELS.items():
        q_count = field_counts["qwen"][key]
        g_count = field_counts["gemma"][key]
        q_correct, denominator, q_rate = score(q_count["correct"], q_count["total"])
        g_correct, g_denominator, g_rate = score(g_count["correct"], g_count["total"])
        if denominator != g_denominator:
            raise ValueError(f"兩模型欄位分母不同：{key}")
        fields.append({
            "key": key,
            "label": label,
            "denominator": denominator,
            "q_correct": q_correct,
            "q_rate": q_rate,
            "g_correct": g_correct,
            "g_rate": g_rate,
        })

    q_field_correct = sum(item["q_correct"] for item in fields)
    g_field_correct = sum(item["g_correct"] for item in fields)
    field_total = sum(item["denominator"] for item in fields)
    q_page = score(full_page["qwen"]["correct"], full_page["qwen"]["total"])
    g_page = score(full_page["gemma"]["correct"], full_page["gemma"]["total"])
    q_core = score(core["qwen"]["correct"], core["qwen"]["total"])
    g_core = score(core["gemma"]["correct"], core["gemma"]["total"])
    prediction_distribution = {}
    for model in ("qwen", "gemma"):
        counts: dict[str, int] = {}
        for key in common_keys:
            predicted = comparison[key][f"{model}_doc_type_key"]
            counts[predicted] = counts.get(predicted, 0) + 1
        prediction_distribution[model] = [
            {
                "key": key,
                "label": DOC_TYPE_LABELS.get(key, key),
                "count": count,
            }
            for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    doc_type_performance = []
    for type_key in DOC_TYPE_ORDER:
        type_keys = [key for key in common_keys if gold[key]["doc_type_key"] == type_key]
        if not type_keys:
            continue
        row = {
            "key": type_key,
            "label": DOC_TYPE_LABELS[type_key],
            "total": len(type_keys),
            "note": DOC_TYPE_NOTES[type_key],
        }
        for model in ("qwen", "gemma"):
            correct = sum(
                comparison[key][f"{model}_doc_type_key"] == type_key for key in type_keys
            )
            row[f"{model}_correct"] = correct
            row[f"{model}_rate"] = correct / len(type_keys) * 100
        doc_type_performance.append(row)
    qwen_doc_type_correct = sum(row["qwen_correct"] for row in doc_type_performance)
    gemma_doc_type_correct = sum(row["gemma_correct"] for row in doc_type_performance)

    return {
        "fields": fields,
        "gold_pages": len(gold_rows),
        "comparison_pages": len(comparison_rows),
        "common_pages": len(common_keys),
        "high_pages": sum(gold[key]["confidence"] == "high" for key in common_keys),
        "medium_pages": sum(gold[key]["confidence"] == "medium" for key in common_keys),
        "low_pages": sum(gold[key]["confidence"] == "low" for key in common_keys),
        "scored_pages": len(scoped_keys),
        "qwen_other": sum(comparison[key]["qwen_doc_type_key"] == "other" for key in common_keys),
        "gemma_other": sum(comparison[key]["gemma_doc_type_key"] == "other" for key in common_keys),
        "qwen_field_wins": sum(item["q_rate"] > item["g_rate"] for item in fields),
        "gemma_field_wins": sum(item["g_rate"] > item["q_rate"] for item in fields),
        "field_ties": sum(item["q_rate"] == item["g_rate"] for item in fields),
        "prediction_distribution": prediction_distribution,
        "doc_type_performance": doc_type_performance,
        "qwen_doc_type_correct": qwen_doc_type_correct,
        "gemma_doc_type_correct": gemma_doc_type_correct,
        "qwen_doc_type_rate_all": qwen_doc_type_correct / len(common_keys) * 100,
        "gemma_doc_type_rate_all": gemma_doc_type_correct / len(common_keys) * 100,
        "q_field_correct": q_field_correct,
        "g_field_correct": g_field_correct,
        "field_total": field_total,
        "q_field_rate": q_field_correct / field_total * 100,
        "g_field_rate": g_field_correct / field_total * 100,
        "q_page": q_page,
        "g_page": g_page,
        "q_core": q_core,
        "g_core": g_core,
    }


def find_cjk_font() -> tuple[str, str]:
    preferred = ("Noto Sans TC", "Microsoft JhengHei", "Arial Unicode MS", "MingLiU")
    entries = font_manager.fontManager.ttflist
    for family in preferred:
        matches = [entry for entry in entries if entry.name == family]
        if not matches:
            continue
        regular = min(matches, key=lambda entry: abs(entry.weight - 400))
        bold = min(matches, key=lambda entry: abs(entry.weight - 700))
        return regular.fname, bold.fname
    fallback = font_manager.findfont(font_manager.FontProperties(family="sans-serif"))
    return fallback, fallback


def create_chart(data: dict, chart_path: Path, regular_font: str, bold_font: str) -> None:
    labels = [item["label"] for item in data["fields"]]
    qwen = [item["q_rate"] for item in data["fields"]]
    gemma = [item["g_rate"] for item in data["fields"]]
    font = font_manager.FontProperties(fname=regular_font)
    font_bold = font_manager.FontProperties(fname=bold_font)

    fig, ax = plt.subplots(figsize=(10, 6.4))
    positions = list(range(len(labels)))
    height = 0.35
    ax.barh([p - height / 2 for p in positions], qwen, height, label="Qwen2.5-VL-7B", color=f"#{GREEN}")
    ax.barh([p + height / 2 for p in positions], gemma, height, label="Gemma4:e4b", color=f"#{ORANGE}")
    ax.set_yticks(positions, labels, fontproperties=font, fontsize=10)
    ax.set_xlim(0, 100)
    ax.set_xlabel("準確率（%）", fontproperties=font, fontsize=10)
    ax.set_title("各關鍵欄位辨識準確率", fontproperties=font_bold, fontsize=15, pad=14)
    ax.grid(axis="x", color=f"#{MID_GRAY}", linewidth=0.7, alpha=0.8)
    ax.set_axisbelow(True)
    ax.invert_yaxis()
    ax.spines[["top", "right", "left"]].set_visible(False)
    for p, value in zip(positions, qwen):
        ax.text(value + 1, p - height / 2, f"{value:.1f}%", va="center", fontsize=8, color=f"#{DARK_GRAY}")
    for p, value in zip(positions, gemma):
        ax.text(value + 1, p + height / 2, f"{value:.1f}%", va="center", fontsize=8, color=f"#{DARK_GRAY}")
    ax.legend(prop=font, loc="lower right", frameon=False)
    fig.tight_layout()
    fig.savefig(chart_path, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_border(cell, color: str = MID_GRAY, size: str = "4") -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = f"w:{edge}"
        element = borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), size)
        element.set(qn("w:color"), color)


def set_run_font(run, name: str = "Microsoft JhengHei", size: int | None = None) -> None:
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if size:
        run.font.size = Pt(size)


def add_docx_table(
    document: Document,
    headers: list[str],
    rows: list[list[str]],
    widths: list[float],
    left_columns: set[int] | None = None,
) -> None:
    left_columns = {0} if left_columns is None else left_columns
    table = document.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for index, (header, width) in enumerate(zip(headers, widths)):
        cell = table.rows[0].cells[index]
        cell.width = Cm(width)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        set_cell_shading(cell, NAVY)
        set_cell_border(cell)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(header)
        set_run_font(run, size=9)
        run.bold = True
        run.font.color.rgb = RGBColor(255, 255, 255)

    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for col_index, (value, width) in enumerate(zip(values, widths)):
            cell = cells[col_index]
            cell.width = Cm(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if row_index % 2:
                set_cell_shading(cell, LIGHT_GRAY)
            set_cell_border(cell)
            paragraph = cell.paragraphs[0]
            paragraph.alignment = (
                WD_ALIGN_PARAGRAPH.LEFT
                if col_index in left_columns
                else WD_ALIGN_PARAGRAPH.RIGHT
            )
            run = paragraph.add_run(value)
            set_run_font(run, size=9)
    document.add_paragraph()


def add_word_footer(section) -> None:
    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("憑證辨識模型準確率比較｜資料來源：validation_comparison.csv、gold_standard.csv")
    set_run_font(run, size=8)
    run.font.color.rgb = RGBColor(107, 114, 128)


def create_docx(data: dict, chart_path: Path) -> None:
    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(1.8)
    section.right_margin = Cm(1.8)
    add_word_footer(section)

    normal = document.styles["Normal"]
    normal.font.name = "Microsoft JhengHei"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft JhengHei")
    normal.font.size = Pt(10)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15
    for style_name, size, color in (
        ("Title", 24, NAVY),
        ("Heading 1", 16, NAVY),
        ("Heading 2", 12, BLUE),
    ):
        style = document.styles[style_name]
        style.font.name = "Microsoft JhengHei"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft JhengHei")
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(color)

    title = document.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.add_run("憑證辨識模型準確率比較")
    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = subtitle.add_run("Qwen2.5-VL-7B 與 Gemma4:e4b 對照人工核定 Gold Standard")
    set_run_font(run, size=11)
    run.font.color.rgb = RGBColor.from_string(DARK_GRAY)
    scope = document.add_paragraph()
    scope.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = scope.add_run(
        f"評估資料：兩份 CSV 共同 {data['common_pages']} 頁｜"
        f"主要結果：{data['scored_pages']} 頁高／中信心答案"
    )
    set_run_font(run, size=9)
    run.font.color.rgb = RGBColor(107, 114, 128)

    document.add_paragraph("摘要", style="Heading 1")
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(0.4)
    paragraph.paragraph_format.right_indent = Cm(0.4)
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(10)
    run = paragraph.add_run(
        f"Qwen 的整體欄位準確率為 {data['q_field_rate']:.1f}%（{data['q_field_correct']:,} / "
        f"{data['field_total']:,}），Gemma 為 {data['g_field_rate']:.1f}%"
        f"（{data['g_field_correct']:,} / {data['field_total']:,}），Qwen 領先 "
        f"{data['q_field_rate'] - data['g_field_rate']:.1f} 個百分點。"
        "嚴格要求同頁所有適用關鍵欄位全對時，兩者分別只有 "
        f"{data['q_page'][2]:.1f}% 與 {data['g_page'][2]:.1f}%。"
    )
    set_run_font(run, size=11)
    run.bold = True

    headline_rows = [
        [
            "整體欄位微平均",
            f"{data['q_field_rate']:.1f}%（{data['q_field_correct']:,}/{data['field_total']:,}）",
            f"{data['g_field_rate']:.1f}%（{data['g_field_correct']:,}/{data['field_total']:,}）",
        ],
        [
            "整頁關鍵欄位全對",
            f"{data['q_page'][2]:.1f}%（{data['q_page'][0]}/{data['q_page'][1]}）",
            f"{data['g_page'][2]:.1f}%（{data['g_page'][0]}/{data['g_page'][1]}）",
        ],
        [
            "日期、憑證號、總額全對",
            f"{data['q_core'][2]:.1f}%（{data['q_core'][0]}/{data['q_core'][1]}）",
            f"{data['g_core'][2]:.1f}%（{data['g_core'][0]}/{data['g_core'][1]}）",
        ],
    ]
    add_docx_table(document, ["指標", "Qwen", "Gemma"], headline_rows, [7.4, 4.8, 4.8])

    document.add_paragraph("評估方法", style="Heading 1")
    methods = [
        "只比較 Gold Standard 有可核實值的欄位；兩模型採相同分母。",
        f"主要結果排除 {data['low_pages']} 頁低信心樣本，使用 "
        f"{data['high_pages']} 頁高信心與 {data['medium_pages']} 頁中信心答案。",
        "金額按數值比較；日期統一格式；憑證號與統編忽略空白及連字號；名稱僅忽略空白。",
        "傳票頁的未稅小計及稅額不列入共通欄位分數；傳票總額採借貸平衡額。",
        "本報告直接從 gold_standard.csv 取得答案，並從 validation_comparison.csv 取得兩模型輸出後重新比對。",
    ]
    for method in methods:
        document.add_paragraph(method, style="List Bullet")

    document.add_page_break()
    document.add_paragraph("各欄位準確率", style="Heading 1")
    document.add_picture(str(chart_path), width=Inches(6.6))
    caption = document.add_paragraph(
        "圖 1　高／中信心答案之欄位準確率；分母依各欄位可核實值數量而異。"
    )
    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in caption.runs:
        set_run_font(run, size=8)
        run.italic = True
        run.font.color.rgb = RGBColor(107, 114, 128)

    field_rows = [
        [
            item["label"],
            str(item["denominator"]),
            f"{item['q_rate']:.1f}%",
            f"{item['g_rate']:.1f}%",
            f"{item['q_rate'] - item['g_rate']:+.1f} pp",
        ]
        for item in data["fields"]
    ]
    add_docx_table(
        document,
        ["欄位", "可計分頁數", "Qwen", "Gemma", "差距（Q−G）"],
        field_rows,
        [5.2, 3.2, 2.8, 2.8, 3.0],
    )

    document.add_page_break()
    document.add_paragraph("四、單據類型（Doc Type）分類表現分析", style="Heading 1")
    paragraph = document.add_paragraph()
    run = paragraph.add_run(
        f"本節採兩份 CSV 的全部 {data['common_pages']} 個共同頁面。"
        f"Gemma4 的分類預測高度集中於 other（{data['gemma_other']} / {data['common_pages']}，"
        f"{data['gemma_other'] / data['common_pages'] * 100:.1f}%），呈現明顯的系統性分類退化；"
        f"Qwen 僅有 {data['qwen_other']} 頁預測為 other。以全部 195 頁計算，"
        f"Qwen 分類正確率為 {data['qwen_doc_type_rate_all']:.2f}%"
        f"（{data['qwen_doc_type_correct']}/{data['common_pages']}），Gemma4 為 "
        f"{data['gemma_doc_type_rate_all']:.2f}%"
        f"（{data['gemma_doc_type_correct']}/{data['common_pages']}）。"
    )
    set_run_font(run)
    run.bold = True

    document.add_paragraph("預測類型分布", style="Heading 2")
    qwen_distribution = {
        row["key"]: row["count"] for row in data["prediction_distribution"]["qwen"]
    }
    gemma_distribution = {
        row["key"]: row["count"] for row in data["prediction_distribution"]["gemma"]
    }
    distribution_keys = sorted(
        set(qwen_distribution) | set(gemma_distribution),
        key=lambda key: (-(qwen_distribution.get(key, 0) + gemma_distribution.get(key, 0)), key),
    )
    distribution_rows = [
        [
            f"{key}（{DOC_TYPE_LABELS.get(key, key)}）",
            str(qwen_distribution.get(key, 0)),
            str(gemma_distribution.get(key, 0)),
        ]
        for key in distribution_keys
    ]
    add_docx_table(
        document,
        ["預測 doc_type_key", "Qwen", "Gemma4"],
        distribution_rows,
        [10.0, 3.5, 3.5],
    )

    document.add_paragraph("依實際類型計算的分類正確率", style="Heading 2")
    type_rows = [
        [
            f"{row['key']}（{row['label']}）",
            str(row["total"]),
            f"{row['qwen_rate']:.2f}%（{row['qwen_correct']}/{row['total']}）",
            f"{row['gemma_rate']:.2f}%（{row['gemma_correct']}/{row['total']}）",
            row["note"],
        ]
        for row in data["doc_type_performance"]
    ]
    add_docx_table(
        document,
        ["實際 doc_type_key", "頁數", "Qwen 正確率", "Gemma4 正確率", "說明"],
        type_rows,
        [4.6, 1.5, 3.0, 3.0, 4.9],
        left_columns={0, 4},
    )

    document.add_paragraph("主要發現", style="Heading 1")
    findings = [
        (
            "Qwen 整體明顯優於 Gemma。",
            f"Qwen 在 {len(data['fields'])} 個欄位中有 {data['qwen_field_wins']} 個較高、"
            f"{data['field_ties']} 個平手；單據類型與買賣方統編的差距尤其明顯。",
        ),
        (
            "Gemma 過度集中判為「其他憑證」。",
            f"在兩份 CSV 的 {data['common_pages']} 個共同頁面中，Gemma 有 "
            f"{data['gemma_other']} 頁輸出 other，Qwen 則有 {data['qwen_other']} 頁。",
        ),
        (
            "名稱辨識是兩模型共同弱點。",
            "買賣方名稱的嚴格字面準確率約 36% 至 39%；品牌、簡稱與法人全名差異也會被計為錯誤。",
        ),
        (
            "多單據及特殊版型容易抓錯金額層級。",
            "同頁多張票、進口稅費、申報書與附件頁容易混淆單張金額、頁面合計、稅額或完稅價格。",
        ),
    ]
    for title_text, detail_text in findings:
        paragraph = document.add_paragraph()
        title_run = paragraph.add_run(title_text + " ")
        set_run_font(title_run)
        title_run.bold = True
        detail_run = paragraph.add_run(detail_text)
        set_run_font(detail_run)

    document.add_paragraph("結論與建議", style="Heading 1")
    conclusion = document.add_paragraph()
    run = conclusion.add_run(
        "若目前必須二選一，建議採用 Qwen 作為主要辨識模型。"
        f"不過整頁所有關鍵欄位全對率僅 {data['q_page'][2]:.1f}%，仍應保留人工覆核，尤其是交易對象名稱、憑證號、"
        "同頁多單據、進口稅費及傳票借貸方向。Gemma 應先改善分類策略，降低大量輸出 other 的情況，再重新評估。"
    )
    set_run_font(run)
    run.bold = True

    document.add_paragraph("解讀限制", style="Heading 1")
    limitations = [
        "Gold Standard 為單次人工判讀的暫定答案，尚未經第二位審核者複核。",
        f"本次只納入兩份 CSV 可依 source 與 page 對應的 {data['common_pages']} 頁。",
        "本報告為逐頁準確率，未進行交易層級去重。",
        "名稱採嚴格字面比對，不能將所有字面差異都解讀為實體辨識錯誤。",
    ]
    for limitation in limitations:
        document.add_paragraph(limitation, style="List Bullet")

    document.add_paragraph()
    source = document.add_paragraph(
        "唯一資料來源：analysis/gold_standard.csv、analysis/validation_comparison.csv"
    )
    for run in source.runs:
        set_run_font(run, size=8)
        run.font.color.rgb = RGBColor(107, 114, 128)

    document.save(DOCX_PATH)


def create_pdf(data: dict, chart_path: Path, regular_font: str, bold_font: str) -> None:
    pdfmetrics.registerFont(TTFont("CJK", regular_font))
    pdfmetrics.registerFont(TTFont("CJK-Bold", bold_font))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="CJK-Bold",
        fontSize=22,
        leading=30,
        textColor=pdf_color(NAVY),
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    subtitle_style = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontName="CJK",
        fontSize=10,
        leading=15,
        textColor=pdf_color(DARK_GRAY),
        alignment=TA_CENTER,
        spaceAfter=16,
    )
    h1_style = ParagraphStyle(
        "H1CJK",
        parent=styles["Heading1"],
        fontName="CJK-Bold",
        fontSize=15,
        leading=21,
        textColor=pdf_color(NAVY),
        spaceBefore=12,
        spaceAfter=8,
    )
    h2_style = ParagraphStyle(
        "H2CJK",
        parent=styles["Heading2"],
        fontName="CJK-Bold",
        fontSize=11,
        leading=16,
        textColor=pdf_color(BLUE),
        spaceBefore=8,
        spaceAfter=5,
    )
    body_style = ParagraphStyle(
        "BodyCJK",
        parent=styles["BodyText"],
        fontName="CJK",
        fontSize=9.5,
        leading=15,
        textColor=pdf_color(DARK_GRAY),
        alignment=TA_LEFT,
        spaceAfter=5,
    )
    body_bold_style = ParagraphStyle(
        "BodyBoldCJK",
        parent=body_style,
        fontName="CJK-Bold",
        fontSize=10.5,
        leading=17,
        backColor=pdf_color(LIGHT_BLUE),
        borderPadding=9,
        spaceBefore=4,
        spaceAfter=12,
    )
    small_style = ParagraphStyle(
        "SmallCJK",
        parent=body_style,
        fontSize=7.5,
        leading=11,
        textColor=pdf_color("6B7280"),
        alignment=TA_CENTER,
    )
    table_header_style = ParagraphStyle(
        "TableHeader",
        parent=body_style,
        fontName="CJK-Bold",
        fontSize=8,
        leading=11,
        textColor=colors.white,
        alignment=TA_CENTER,
    )
    table_cell_style = ParagraphStyle(
        "TableCell",
        parent=body_style,
        fontSize=8,
        leading=11,
        spaceAfter=0,
    )
    table_num_style = ParagraphStyle(
        "TableNum",
        parent=table_cell_style,
        alignment=TA_CENTER,
    )

    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title="憑證辨識模型準確率比較",
        author="Cursor",
        subject="Qwen2.5-VL-7B 與 Gemma4:e4b 準確率分析",
    )
    story = [
        Spacer(1, 0.6 * cm),
        Paragraph("憑證辨識模型準確率比較", title_style),
        Paragraph(
            "Qwen2.5-VL-7B 與 Gemma4:e4b 對照人工核定 Gold Standard<br/>"
            f"評估資料：兩份 CSV 共同 {data['common_pages']} 頁｜"
            f"主要結果：{data['scored_pages']} 頁高／中信心答案",
            subtitle_style,
        ),
        Paragraph("摘要", h1_style),
        Paragraph(
            f"Qwen 的整體欄位準確率為 {data['q_field_rate']:.1f}%（{data['q_field_correct']:,} / "
            f"{data['field_total']:,}），Gemma 為 {data['g_field_rate']:.1f}%"
            f"（{data['g_field_correct']:,} / {data['field_total']:,}），Qwen 領先 "
            f"{data['q_field_rate'] - data['g_field_rate']:.1f} 個百分點。"
            "嚴格要求同頁所有適用關鍵欄位全對時，兩者分別只有 "
            f"{data['q_page'][2]:.1f}% 與 {data['g_page'][2]:.1f}%。",
            body_bold_style,
        ),
    ]

    headline_data = [
        [
            Paragraph("指標", table_header_style),
            Paragraph("Qwen", table_header_style),
            Paragraph("Gemma", table_header_style),
        ],
        [
            Paragraph("整體欄位微平均", table_cell_style),
            Paragraph(f"{data['q_field_rate']:.1f}%<br/>({data['q_field_correct']:,}/{data['field_total']:,})", table_num_style),
            Paragraph(f"{data['g_field_rate']:.1f}%<br/>({data['g_field_correct']:,}/{data['field_total']:,})", table_num_style),
        ],
        [
            Paragraph("整頁關鍵欄位全對", table_cell_style),
            Paragraph(f"{data['q_page'][2]:.1f}% ({data['q_page'][0]}/{data['q_page'][1]})", table_num_style),
            Paragraph(f"{data['g_page'][2]:.1f}% ({data['g_page'][0]}/{data['g_page'][1]})", table_num_style),
        ],
        [
            Paragraph("日期、憑證號、總額全對", table_cell_style),
            Paragraph(f"{data['q_core'][2]:.1f}% ({data['q_core'][0]}/{data['q_core'][1]})", table_num_style),
            Paragraph(f"{data['g_core'][2]:.1f}% ({data['g_core'][0]}/{data['g_core'][1]})", table_num_style),
        ],
    ]
    headline_table = Table(headline_data, colWidths=[8.2 * cm, 4.5 * cm, 4.5 * cm])
    headline_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), pdf_color(NAVY)),
                ("GRID", (0, 0), (-1, -1), 0.4, pdf_color(MID_GRAY)),
                ("BACKGROUND", (0, 2), (-1, 2), pdf_color(LIGHT_GRAY)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend(
        [
            headline_table,
            Spacer(1, 0.25 * cm),
            Paragraph("評估方法", h1_style),
        ]
    )
    methods = [
        "只比較 Gold Standard 有可核實值的欄位；兩模型採相同分母。",
        f"主要結果排除 {data['low_pages']} 頁低信心樣本，使用 "
        f"{data['high_pages']} 頁高信心與 {data['medium_pages']} 頁中信心答案。",
        "金額按數值比較；日期統一格式；憑證號與統編忽略空白及連字號；名稱僅忽略空白。",
        "傳票頁的未稅小計及稅額不列入共通欄位分數；傳票總額採借貸平衡額。",
        "本報告直接從 gold_standard.csv 取得答案，並從 validation_comparison.csv 取得兩模型輸出後重新比對。",
    ]
    for method in methods:
        story.append(Paragraph(f"•　{method}", body_style))

    story.extend(
        [
            PageBreak(),
            Paragraph("各欄位準確率", h1_style),
            Image(str(chart_path), width=17.4 * cm, height=11.1 * cm),
            Paragraph("圖 1　高／中信心答案之欄位準確率；分母依欄位可核實值數量而異。", small_style),
            Spacer(1, 0.2 * cm),
        ]
    )
    field_data = [
        [
            Paragraph("欄位", table_header_style),
            Paragraph("可計分頁數", table_header_style),
            Paragraph("Qwen", table_header_style),
            Paragraph("Gemma", table_header_style),
            Paragraph("差距（Q−G）", table_header_style),
        ]
    ]
    for item in data["fields"]:
        field_data.append(
            [
                Paragraph(item["label"], table_cell_style),
                Paragraph(str(item["denominator"]), table_num_style),
                Paragraph(f"{item['q_rate']:.1f}%", table_num_style),
                Paragraph(f"{item['g_rate']:.1f}%", table_num_style),
                Paragraph(f"{item['q_rate'] - item['g_rate']:+.1f} pp", table_num_style),
            ]
        )
    field_table = Table(field_data, colWidths=[4.7 * cm, 3.2 * cm, 3 * cm, 3 * cm, 3.3 * cm], repeatRows=1)
    field_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), pdf_color(NAVY)),
        ("GRID", (0, 0), (-1, -1), 0.35, pdf_color(MID_GRAY)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row in range(2, len(field_data), 2):
        field_styles.append(("BACKGROUND", (0, row), (-1, row), pdf_color(LIGHT_GRAY)))
    field_table.setStyle(TableStyle(field_styles))
    story.append(field_table)

    story.extend(
        [
            PageBreak(),
            Paragraph("四、單據類型（Doc Type）分類表現分析", h1_style),
            Paragraph(
                f"本節採兩份 CSV 的全部 {data['common_pages']} 個共同頁面。"
                f"Gemma4 的分類預測高度集中於 other（{data['gemma_other']} / {data['common_pages']}，"
                f"{data['gemma_other'] / data['common_pages'] * 100:.1f}%），呈現明顯的系統性分類退化；"
                f"Qwen 僅有 {data['qwen_other']} 頁預測為 other。",
                body_bold_style,
            ),
            Paragraph(
                "以全部 195 頁計算，"
                f"Qwen 分類正確率為 {data['qwen_doc_type_rate_all']:.2f}%"
                f"（{data['qwen_doc_type_correct']}/{data['common_pages']}），Gemma4 為 "
                f"{data['gemma_doc_type_rate_all']:.2f}%"
                f"（{data['gemma_doc_type_correct']}/{data['common_pages']}）。",
                body_style,
            ),
            Paragraph("預測類型分布", h2_style),
        ]
    )
    qwen_distribution = {
        row["key"]: row["count"] for row in data["prediction_distribution"]["qwen"]
    }
    gemma_distribution = {
        row["key"]: row["count"] for row in data["prediction_distribution"]["gemma"]
    }
    distribution_keys = sorted(
        set(qwen_distribution) | set(gemma_distribution),
        key=lambda key: (-(qwen_distribution.get(key, 0) + gemma_distribution.get(key, 0)), key),
    )
    distribution_data = [
        [
            Paragraph("預測 doc_type_key", table_header_style),
            Paragraph("Qwen", table_header_style),
            Paragraph("Gemma4", table_header_style),
        ],
    ]
    for key in distribution_keys:
        distribution_data.append(
            [
                Paragraph(f"{key}（{DOC_TYPE_LABELS.get(key, key)}）", table_cell_style),
                Paragraph(str(qwen_distribution.get(key, 0)), table_num_style),
                Paragraph(str(gemma_distribution.get(key, 0)), table_num_style),
            ]
        )
    distribution_table = Table(
        distribution_data,
        colWidths=[9 * cm, 4.1 * cm, 4.1 * cm],
        repeatRows=1,
    )
    distribution_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), pdf_color(NAVY)),
                ("GRID", (0, 0), (-1, -1), 0.4, pdf_color(MID_GRAY)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(distribution_table)
    story.append(Paragraph("依實際類型計算的分類正確率", h2_style))
    type_data = [
        [
            Paragraph("實際 doc_type_key", table_header_style),
            Paragraph("頁數", table_header_style),
            Paragraph("Qwen 正確率", table_header_style),
            Paragraph("Gemma4 正確率", table_header_style),
            Paragraph("說明", table_header_style),
        ]
    ]
    for row in data["doc_type_performance"]:
        type_data.append(
            [
                Paragraph(f"{row['key']}<br/>（{row['label']}）", table_cell_style),
                Paragraph(str(row["total"]), table_num_style),
                Paragraph(
                    f"{row['qwen_rate']:.2f}%<br/>({row['qwen_correct']}/{row['total']})",
                    table_num_style,
                ),
                Paragraph(
                    f"{row['gemma_rate']:.2f}%<br/>({row['gemma_correct']}/{row['total']})",
                    table_num_style,
                ),
                Paragraph(row["note"], table_cell_style),
            ]
        )
    type_table = Table(
        type_data,
        colWidths=[4.1 * cm, 1.2 * cm, 2.8 * cm, 2.8 * cm, 6.3 * cm],
        repeatRows=1,
    )
    type_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), pdf_color(NAVY)),
        ("GRID", (0, 0), (-1, -1), 0.35, pdf_color(MID_GRAY)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_index in range(2, len(type_data), 2):
        type_styles.append(
            ("BACKGROUND", (0, row_index), (-1, row_index), pdf_color(LIGHT_GRAY))
        )
    type_table.setStyle(TableStyle(type_styles))
    story.append(type_table)
    story.append(Paragraph("主要發現", h1_style))
    findings = [
        (
            "Qwen 整體明顯優於 Gemma",
            f"Qwen 在 {len(data['fields'])} 個欄位中有 {data['qwen_field_wins']} 個較高、"
            f"{data['field_ties']} 個平手；單據類型與買賣方統編的差距尤其明顯。",
        ),
        (
            "Gemma 過度集中判為「其他憑證」",
            f"在兩份 CSV 的 {data['common_pages']} 個共同頁面中，Gemma 有 "
            f"{data['gemma_other']} 頁輸出 other，Qwen 則有 {data['qwen_other']} 頁。",
        ),
        (
            "名稱辨識是兩模型共同弱點",
            "買賣方名稱的嚴格字面準確率約 36% 至 39%；品牌、簡稱與法人全名差異也會被計為錯誤。",
        ),
        (
            "多單據及特殊版型容易抓錯金額層級",
            "同頁多張票、進口稅費、申報書與附件頁容易混淆單張金額、頁面合計、稅額或完稅價格。",
        ),
    ]
    for title_text, detail_text in findings:
        story.append(Paragraph(title_text, h2_style))
        story.append(Paragraph(detail_text, body_style))

    story.extend(
        [
            Paragraph("結論與建議", h1_style),
            Paragraph(
                "若目前必須二選一，建議採用 Qwen 作為主要辨識模型。"
                f"不過整頁所有關鍵欄位全對率僅 {data['q_page'][2]:.1f}%，仍應保留人工覆核，尤其是交易對象名稱、憑證號、"
                "同頁多單據、進口稅費及傳票借貸方向。Gemma 應先改善分類策略，降低大量輸出 other 的情況，再重新評估。",
                body_bold_style,
            ),
            Paragraph("解讀限制", h1_style),
        ]
    )
    limitations = [
        "Gold Standard 為單次人工判讀的暫定答案，尚未經第二位審核者複核。",
        f"本次只納入兩份 CSV 可依 source 與 page 對應的 {data['common_pages']} 頁。",
        "本報告為逐頁準確率，未進行交易層級去重。",
        "名稱採嚴格字面比對，不能將所有字面差異都解讀為實體辨識錯誤。",
    ]
    for limitation in limitations:
        story.append(Paragraph(f"•　{limitation}", body_style))
    story.extend(
        [
            Spacer(1, 0.3 * cm),
            Paragraph(
                "唯一資料來源：analysis/gold_standard.csv、analysis/validation_comparison.csv",
                small_style,
            ),
        ]
    )

    def page_footer(canvas, document):
        canvas.saveState()
        canvas.setFont("CJK", 7)
        canvas.setFillColor(pdf_color("6B7280"))
        canvas.drawString(1.5 * cm, 0.8 * cm, "憑證辨識模型準確率比較")
        canvas.drawRightString(A4[0] - 1.5 * cm, 0.8 * cm, f"第 {document.page} 頁")
        canvas.restoreState()

    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = load_report_data()
    regular_font, bold_font = find_cjk_font()
    with tempfile.TemporaryDirectory() as temp_dir:
        chart_path = Path(temp_dir) / "accuracy_chart.png"
        create_chart(data, chart_path, regular_font, bold_font)
        create_docx(data, chart_path)
        create_pdf(data, chart_path, regular_font, bold_font)
    print(f"PDF: {PDF_PATH} ({PDF_PATH.stat().st_size:,} bytes)")
    print(f"Word: {DOCX_PATH} ({DOCX_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
