"""Build the user manual PDF from the supported content in its HTML source.

ReportLab is imported only when a build is requested so parser/path tests and
--help work in the lightweight application environment.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from typing import Iterable
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "docs" / "manual" / "使用者手冊.html"
DEFAULT_OUTPUT = ROOT / "docs" / "manual" / "使用者手冊.pdf"
DEFAULT_FONT = Path(r"C:\Windows\Fonts\msjh.ttc")
DEFAULT_BOLD_FONT = Path(r"C:\Windows\Fonts\msjhbd.ttc")

BLOCK_TAGS = {"main", "section", "header", "footer", "h1", "h2", "h3", "p", "ol", "ul", "li", "table", "thead", "tbody", "tr", "th", "td", "figure", "figcaption"}
INLINE_TAGS = {"strong", "b", "em", "i", "br", "a", "span", "code"}
VOID_TAGS = {"br", "img"}
SKIP_TAGS = {"script", "style", "iframe", "object", "embed", "svg", "canvas", "template", "dialog", "button"}


@dataclass
class Element:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element | str"] = field(default_factory=list)


class ManualParser(HTMLParser):
    """Extract a safe, small document tree from only ``main#main``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("root")
        self.stack = [self.root]
        self.in_main = False
        self.main_found = False
        self.skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if self.skip_stack:
            if tag not in {"img", "br", "meta", "link", "input", "hr", "source", "wbr"}:
                self.skip_stack.append(tag)
            return
        if not self.in_main:
            if tag == "main" and attr_map.get("id") == "main":
                self.in_main = True
                self.main_found = True
                main = Element("main", attr_map)
                self.root.children.append(main)
                self.stack = [self.root, main]
            return
        if tag in SKIP_TAGS:
            self.skip_stack.append(tag)
            return
        if tag == "img":
            self.stack[-1].children.append(Element("img", {k: v for k, v in attr_map.items() if k in {"src", "alt", "title"}}))
            return
        if tag not in BLOCK_TAGS and tag not in INLINE_TAGS:
            # Unknown wrappers are transparent: retain their text, discard attributes.
            return
        element = Element(tag, attr_map if tag in {"ol", "ul", "a", "figure"} else {})
        self.stack[-1].children.append(element)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_stack:
            for index in range(len(self.skip_stack) - 1, -1, -1):
                if self.skip_stack[index] == tag:
                    del self.skip_stack[index:]
                    break
            return
        if not self.in_main:
            return
        if tag == "main":
            self.in_main = False
            self.stack = [self.root]
            return
        if tag not in BLOCK_TAGS and tag not in INLINE_TAGS:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.in_main and not self.skip_stack and data:
            self.stack[-1].children.append(data)


def parse_manual(source: str) -> Element:
    """Parse supported content from the HTML source and return its main node."""
    parser = ManualParser()
    parser.feed(source)
    parser.close()
    if not parser.main_found:
        raise ValueError("HTML source does not contain <main id='main'>")
    return next(child for child in parser.root.children if isinstance(child, Element) and child.tag == "main")


def safe_image_path(src: str, image_root: Path) -> Path:
    """Resolve a manual ``img/...`` source while preventing traversal/network paths."""
    if not src:
        raise ValueError("figure image is missing its src attribute")
    parsed = urlsplit(src)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in parsed.path:
        raise ValueError(f"unsupported image URL (expected a local img/ path): {src}")
    decoded = unquote(parsed.path)
    parts = decoded.split("/")
    if len(parts) < 2 or parts[0] != "img" or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"image path must stay inside the manual img directory: {src}")
    root = image_root.resolve()
    path = (root / Path(*parts[1:])).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"image path escapes the manual img directory: {src}")
    if not path.is_file():
        raise FileNotFoundError(f"manual image not found: {path}")
    return path


def _walk(node: Element) -> Iterable[Element]:
    yield node
    for child in node.children:
        if isinstance(child, Element):
            yield from _walk(child)


def validate_images(main: Element, image_root: Path) -> dict[int, Path]:
    """Validate every embedded image before a PDF output file can be opened."""
    result: dict[int, Path] = {}
    for element in _walk(main):
        if element.tag == "img":
            result[id(element)] = safe_image_path(element.attrs.get("src", ""), image_root)
    return result


def _plain_text(node: Element | str) -> str:
    if isinstance(node, str):
        return node
    return "".join(_plain_text(child) for child in node.children)


def _markup(node: Element | str) -> str:
    from xml.sax.saxutils import escape

    if isinstance(node, str):
        return escape(node)
    if node.tag == "br":
        return "<br/>"
    content = "".join(_markup(child) for child in node.children)
    if node.tag in {"strong", "b"}:
        return f"<b>{content}</b>"
    if node.tag in {"em", "i"}:
        return f"<i>{content}</i>"
    return content


def _descendants(node: Element, tag: str) -> list[Element]:
    return [item for item in _walk(node) if item is not node and item.tag == tag]


def build_pdf(source: Path, output: Path, font: Path, bold_font: Path) -> None:
    """Build the requested PDF. ReportLab is intentionally loaded only here."""
    source = source.resolve()
    output = output.resolve()
    font = font.resolve()
    bold_font = bold_font.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"manual HTML source not found: {source}")
    if not font.is_file():
        raise FileNotFoundError(f"regular Chinese font not found: {font}")
    if not bold_font.is_file():
        raise FileNotFoundError(f"bold Chinese font not found: {bold_font}")
    main = parse_manual(source.read_text(encoding="utf-8"))
    image_root = source.parent / "img"
    images = validate_images(main, image_root)

    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            BaseDocTemplate, Frame, Image, KeepTogether, LongTable, PageBreak,
            PageTemplate, Paragraph, Spacer, TableStyle,
        )
        from reportlab.platypus.tableofcontents import TableOfContents
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        raise RuntimeError("ReportLab is required to build the PDF; use the bundled Python runtime") from exc

    normal_name, bold_name = "ManualCJK", "ManualCJK-Bold"
    normal_args = {"subfontIndex": 0} if font.suffix.lower() == ".ttc" else {}
    bold_args = {"subfontIndex": 0} if bold_font.suffix.lower() == ".ttc" else {}
    pdfmetrics.registerFont(TTFont(normal_name, str(font), **normal_args))
    pdfmetrics.registerFont(TTFont(bold_name, str(bold_font), **bold_args))
    pdfmetrics.registerFontFamily(normal_name, normal=normal_name, bold=bold_name, italic=normal_name, boldItalic=bold_name)

    page_w, page_h = A4
    left, right, top, bottom = 21 * mm, 21 * mm, 19 * mm, 19 * mm
    usable_w = page_w - left - right
    frame_h = page_h - top - bottom
    styles = getSampleStyleSheet()
    base = ParagraphStyle("ManualBody", parent=styles["BodyText"], fontName=normal_name, fontSize=10.5,
                          leading=17, textColor=colors.HexColor("#1d2939"), spaceAfter=7,
                          wordWrap="CJK", allowWidows=0, allowOrphans=0)
    headings = {
        "h1": ParagraphStyle("ManualH1", parent=base, fontName=bold_name, fontSize=22, leading=31,
                             alignment=TA_CENTER, spaceBefore=4, spaceAfter=16, keepWithNext=True),
        "h2": ParagraphStyle("ManualH2", parent=base, fontName=bold_name, fontSize=17, leading=25,
                             spaceBefore=16, spaceAfter=8, keepWithNext=True, textColor=colors.HexColor("#1c5870")),
        "h3": ParagraphStyle("ManualH3", parent=base, fontName=bold_name, fontSize=12.5, leading=19,
                             spaceBefore=11, spaceAfter=5, keepWithNext=True),
    }
    caption_style = ParagraphStyle("ManualCaption", parent=base, fontSize=9, leading=14,
                                   alignment=TA_CENTER, textColor=colors.HexColor("#526176"), spaceBefore=5, spaceAfter=9)
    list_style = ParagraphStyle("ManualList", parent=base, spaceAfter=2)
    toc_title = ParagraphStyle("ManualTOCTitle", parent=headings["h2"], alignment=TA_LEFT, keepWithNext=True)
    toc_styles = [
        ParagraphStyle("ManualTOC1", parent=base, fontName=bold_name, fontSize=10.5, leading=18, leftIndent=0, firstLineIndent=0, spaceBefore=3),
        ParagraphStyle("ManualTOC2", parent=base, fontSize=9.5, leading=15, leftIndent=12 * mm, firstLineIndent=0),
    ]

    class Heading(Paragraph):
        def __init__(self, text: str, style: ParagraphStyle, level: int, key: str):
            super().__init__(text, style)
            self.manual_level = level
            self.manual_key = key

    class ManualDoc(BaseDocTemplate):
        def afterFlowable(self, flowable):  # noqa: N802 - ReportLab callback name
            level = getattr(flowable, "manual_level", None)
            if level is None:
                return
            key = flowable.manual_key
            title = _plain_text_from_markup(flowable.getPlainText())
            self.canv.bookmarkPage(key)
            outline_level = max(0, level - 1)
            self.canv.addOutlineEntry(title, key, level=outline_level, closed=False)
            if level in (2, 3):
                self.notify("TOCEntry", (level - 2, title, self.page, key))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(normal_name, 8)
        canvas.setFillColor(colors.HexColor("#526176"))
        canvas.drawCentredString(page_w / 2, 10 * mm, f"憑證辨識系統使用者手冊　·　{doc.page}")
        canvas.restoreState()

    doc = ManualDoc(str(output), pagesize=A4, leftMargin=left, rightMargin=right, topMargin=top, bottomMargin=bottom,
                    title="憑證辨識系統使用者手冊", author="憑證辨識系統")
    frame = Frame(left, bottom, usable_w, frame_h, id="manual", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="manual", frames=[frame], onPage=footer)])
    toc = TableOfContents()
    toc.levelStyles = toc_styles
    toc.dotsMinLevel = 0
    story: list[object] = []
    heading_count = 0
    figure_height_limit = frame_h * 0.62

    def flow_for(element: Element) -> list[object]:
        nonlocal heading_count
        tag = element.tag
        if tag in {"h1", "h2", "h3"}:
            heading_count += 1
            level = int(tag[1])
            key = f"manual-heading-{heading_count}"
            return [Heading(_markup(element), headings[tag], level, key)]
        if tag == "p":
            markup = _markup(element).strip()
            return [Paragraph(markup, base), Spacer(1, 1)] if markup else []
        if tag in {"ol", "ul"}:
            if "flow" in element.attrs.get("class", "").split():
                return [Paragraph(_markup(li).replace("</b>", "</b>　", 1), list_style)
                        for li in element.children if isinstance(li, Element) and li.tag == "li"]
            items = []
            for li in (child for child in element.children if isinstance(child, Element) and child.tag == "li"):
                contents: list[object] = []
                inline: list[Element | str] = []
                for child in li.children:
                    if isinstance(child, Element) and child.tag in {"ol", "ul"}:
                        if inline:
                            contents.append(Paragraph("".join(_markup(part) for part in inline), list_style))
                            inline = []
                        contents.extend(flow_for(child))
                    else:
                        inline.append(child)
                if inline:
                    contents.insert(0, Paragraph("".join(_markup(part) for part in inline).strip(), list_style))
                if contents:
                    from reportlab.platypus import ListItem, ListFlowable
                    items.append(ListItem(contents, leftIndent=2 * mm))
            if not items:
                return []
            from reportlab.platypus import ListFlowable
            return [ListFlowable(items, bulletType="1" if tag == "ol" else "bullet",
                                 start=str(element.attrs.get("start", "1")) if tag == "ol" else "•",
                                 bulletFontName=normal_name, bulletFontSize=10, leftIndent=7 * mm,
                                 bulletDedent=3 * mm, spaceBefore=2, spaceAfter=8)]
        if tag == "table":
            rows = [row for row in _descendants(element, "tr")]
            data = []
            header_rows = 0
            for row in rows:
                cells = [cell for cell in row.children if isinstance(cell, Element) and cell.tag in {"th", "td"}]
                if not cells:
                    continue
                if not data and any(cell.tag == "th" for cell in cells):
                    header_rows = 1
                data.append([Paragraph(_markup(cell).strip() or " ", ParagraphStyle(
                    f"ManualTableCell{len(data)}{i}", parent=base, fontSize=8.5, leading=13, spaceAfter=0,
                    wordWrap="CJK", fontName=bold_name if cell.tag == "th" else normal_name,
                )) for i, cell in enumerate(cells)])
            if not data:
                return []
            col_count = max(len(row) for row in data)
            for row in data:
                row.extend([Paragraph(" ", base)] * (col_count - len(row)))
            widths = [usable_w / col_count] * col_count
            table = LongTable(data, colWidths=widths, repeatRows=header_rows, splitByRow=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#cdd6df")),
                ("BACKGROUND", (0, 0), (-1, header_rows - 1), colors.HexColor("#e5edf2")) if header_rows else ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            return [table, Spacer(1, 8)]
        if tag == "figure":
            parts: list[object] = []
            for image in _descendants(element, "img"):
                path = images[id(image)]
                width, height = ImageReader(str(path)).getSize()
                scale = min(usable_w / width, figure_height_limit / height, 1.0)
                parts.append(Image(str(path), width=width * scale, height=height * scale, hAlign="CENTER"))
            for caption in _descendants(element, "figcaption"):
                markup = _markup(caption).strip()
                if markup:
                    parts.append(Paragraph(markup, caption_style))
            if not parts:
                return []
            return [KeepTogether(parts), Spacer(1, 8)]
        if tag in {"section", "header", "footer", "main"}:
            result: list[object] = []
            for child in element.children:
                if isinstance(child, Element):
                    if child.tag == "figure" or child.tag in {"h1", "h2", "h3", "p", "ol", "ul", "table", "section", "header", "footer"}:
                        result.extend(flow_for(child))
            return result
        if tag == "figure":
            return []
        return []

    # Keep the source's title and introduction first, then insert a generated TOC
    # immediately before the first chapter heading.
    toc_inserted = False
    for child in main.children:
        if not isinstance(child, Element):
            continue
        if child.tag == "section" and not toc_inserted:
            story.extend([PageBreak(), Paragraph("目錄", toc_title), toc, PageBreak()])
            toc_inserted = True
        story.extend(flow_for(child))
    if not toc_inserted:
        story = [Paragraph("目錄", toc_title), toc, PageBreak(), *story]
    if not heading_count:
        raise ValueError("main content has no supported headings")
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.multiBuild(story, maxPasses=10)


def _plain_text_from_markup(value: str) -> str:
    return re.sub(r"<[^>]*>", "", value).strip()


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate the user manual PDF from docs/manual/使用者手冊.html")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help=f"source HTML (default: {DEFAULT_SOURCE})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"output PDF (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT, help=f"regular Chinese font (default: {DEFAULT_FONT})")
    parser.add_argument("--bold-font", type=Path, default=DEFAULT_BOLD_FONT, help=f"bold Chinese font (default: {DEFAULT_BOLD_FONT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        build_pdf(args.source, args.output, args.font, args.bold_font)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"build_user_manual: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
