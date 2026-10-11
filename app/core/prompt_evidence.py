"""One compact OCR representation for prompts; raw evidence stays in the journal."""
from __future__ import annotations
from PIL import Image

VERSION = 1


def assemble(ocr_text="", evidence=None, image_path=None):
    evidence = evidence or {}
    # Reread positions belong to the ROI, not the original page. Keep copies apart.
    lines = evidence.get("supplement_prompt_lines") if "supplement_prompt_lines" in evidence else evidence.get("supplement_ocr_lines") if "supplement_ocr_lines" in evidence else evidence.get("ocr_lines")
    result = {k:evidence[k] for k in ("company", "company_name", "qr") if k in evidence}
    if lines:
        size = evidence.get("supplement_ocr_size") if "supplement_ocr_lines" in evidence else evidence.get("ocr_size")
        if size:
            w, h = size
        else:
            with Image.open(image_path) as image:
                w, h = image.size
        compact = []
        for index, line in enumerate(lines):
            box = line.get("box") or line.get("bbox")
            if box and isinstance(box[0], (list,tuple)):
                xs, ys = zip(*box)
                box = [min(xs),min(ys),max(xs),max(ys)]
            normalized = [round(box[i]*1000/(w if i%2==0 else h)) for i in range(4)] if box and len(box)==4 else None
            compact.append({"id":index,"text":line.get("text", ""),"box":normalized})
        result["ocr_lines"] = compact
        result["ocr_frame"] = "supplement_roi_normalized_1000" if "supplement_ocr_lines" in evidence else "document_normalized_1000"
        if evidence.get("ocr_copy_conflict"):
            result["ocr_copy_conflict"] = True
    else:
        # Text-only callers without positions retain the necessary verbatim text.
        result["ocr_text"] = ocr_text
    return result
