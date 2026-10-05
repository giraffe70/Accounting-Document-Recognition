from pathlib import Path

from PIL import Image, ImageDraw


SOURCE = Path("artifacts/_full_report_render/pages_pass4")
OUTPUT = Path("artifacts/_full_report_render/contact_sheets_pass4")
COLS, ROWS = 3, 3
THUMB = (360, 278)
MARGIN = 16


def page_number(path: Path) -> int:
    return int(path.stem.split("-")[1])


def main():
    pages = sorted(SOURCE.glob("page-*.png"), key=page_number)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for start in range(0, len(pages), COLS * ROWS):
        group = pages[start : start + COLS * ROWS]
        width = COLS * (THUMB[0] + MARGIN) + MARGIN
        height = ROWS * (THUMB[1] + 38) + MARGIN
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        for index, path in enumerate(group):
            col, row = index % COLS, index // COLS
            x = MARGIN + col * (THUMB[0] + MARGIN)
            y = MARGIN + row * (THUMB[1] + 38)
            with Image.open(path) as image:
                canvas.paste(image.convert("RGB").resize(THUMB), (x, y + 18))
            draw.text((x, y), f"Page {page_number(path)}", fill=(20, 20, 20))
        canvas.save(OUTPUT / f"sheet-{start // (COLS * ROWS) + 1}.png")


if __name__ == "__main__":
    main()
