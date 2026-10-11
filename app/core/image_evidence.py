"""Source-only layout and OCR image evidence; never edits an original image."""
from pathlib import Path
import re
import math
from PIL import Image
from app.core import invocation_trace
from app.core.geometry import iou


def native_ocr(source, target):
    with Image.open(source) as image:
        original_size = image.size
        image = image.convert("RGB")
        image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        image.save(target)
        size = image.size
    invocation_trace.add({"kind": "ocr_input", "source_size": original_size, "input_size": size,
                          "rotation": 0, "coordinate_unit": "pixels", "path": str(target)})
    return str(target)


def map_lines(lines, source_size, target_size):
    """Explicit pixel-to-pixel mapping for an already upright native crop."""
    sx, sy = target_size[0] / source_size[0], target_size[1] / source_size[1]
    invocation_trace.add({"kind":"ocr_coordinate_transform", "source_size":source_size, "target_size":target_size,
                          "source_unit":"pixels", "target_unit":"pixels", "matrix":[[sx,0,0],[0,sy,0]]})
    return [{**line, "box": [[p[0]*sx, p[1]*sy] for p in line["box"]]} for line in lines]


def frames(source):
    """Closed rectangular source borders, excluding the page and nested copies."""
    import cv2
    import numpy as np
    with Image.open(source) as image:
        image = image.convert("L")
        image.thumbnail((1600, 1600))
        gray = np.asarray(image)
    h, w = gray.shape
    contours, _ = cv2.findContours((gray < 180).astype("uint8")*255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = bw*bh/(w*h)
        approx = cv2.approxPolyDP(contour, .02*cv2.arcLength(contour, True), True)
        if (len(approx) == 4 and cv2.isContourConvex(approx) and .04 <= area < .95
                and bw/w >= .12 and bh/h >= .12 and max(bw/bh, bh/bw) < 8
                and abs(cv2.contourArea(contour))/(bw*bh) > .85):
            # A closed text cell or an empty border is not a complete document.
            margin = max(3,round(min(bw,bh)*.02))
            interior = gray[y+margin:y+bh-margin, x+margin:x+bw-margin] < 180
            rows = interior.sum(axis=1) > max(2,bw*.005)
            runs = int(np.sum(rows & ~np.r_[False, rows[:-1]]))
            if interior.size == 0 or runs < 3 or interior.mean() < .001:
                continue
            box = [round(1000*x/w), round(1000*y/h), round(1000*(x+bw)/w), round(1000*(y+bh)/h)]
            found.append(box)
    # Multiple complete inner documents defeat an enclosing page frame.
    candidates = []
    for parent in found:
        children = [b for b in found if b != parent and parent[0]+8 < b[0] and parent[1]+8 < b[1] and parent[2]-8 > b[2] and parent[3]-8 > b[3]]
        independent = []
        for b in children:
            if not any(iou(b,c) > .1 for c in independent):
                independent.append(b)
        if len(independent) < 2:
            candidates.append(parent)
    found = candidates
    # A receipt's internal cells are not separate documents.
    found.sort(key=lambda b: (b[2]-b[0])*(b[3]-b[1]), reverse=True)
    outer = []
    for box in found:
        if not any((a[0]-8 <= box[0] and a[1]-8 <= box[1] and a[2]+8 >= box[2] and a[3]+8 >= box[3]) or iou(a, box) > .5 for a in outer):
            outer.append(box)
    return outer


def anchors(lines, size):
    result = []
    for line in lines or []:
        text = line.get("text", "")
        ids = re.findall(r"(?<![A-Za-z0-9])[A-Za-z]{2}\s*\d{8}(?!\d)|(?:票號|支票號碼|發票號碼)\s*[:：]?\s*\d{5,12}", text)
        amount = bool(re.search(r"(?:總計|總額|合計|新[臺台]幣|NT\$|金額|大寫).*(?:\d|[壹貳參肆伍陸柒捌玖])", text))
        if ids or amount:
            points = line["box"]
            result.append({"ids": [re.sub(r"\s+", "", v).upper() for v in ids], "amount": amount,
                           "center": [sum(p[0] for p in points)/len(points)*1000/size[0], sum(p[1] for p in points)/len(points)*1000/size[1]]})
    return result


def layout_evidence(source=None, lines=None, size=None, qr=None):
    borders = frames(source) if source else []
    identified = anchors(lines, size) if size else []
    for detection in (qr or {}).get("detections", []):
        if len(detection.get("candidates", [])) != 1:
            continue
        box = detection["box"]
        w,h = qr["source_size"]
        candidate = detection["candidates"][0]
        identified.append({"ids":[candidate["voucher_id"]], "amount":True, "source":"qr", "type":"invoice",
                           "center":[(box[0]+box[2])*500/w,(box[1]+box[3])*500/h]})
    source_size = size
    if source:
        with Image.open(source) as image:
            source_size = image.size
    return {"frames": borders, "anchors": identified, "coordinate_unit":"normalized_1000",
            "source_size":source_size, "ocr_source_size":size}


def verify_regions(regions, evidence):
    issues, used_ids, used_frames = [], set(), set()
    if len(regions) == 1 and len(evidence["frames"]) > 1:
        issues.append("single region conflicts with multiple independent document boundaries")
    for index, region in enumerate(regions):
        x0, y0, x1, y1 = region["box"]
        if min(x1-x0, y1-y0) < 120 or max((x1-x0)/(y1-y0), (y1-y0)/(x1-x0)) > 8:
            issues.append(f"region {index}: text strip or incomplete document")
        borders = [(i, b) for i,b in enumerate(evidence["frames"]) if iou(b, region["box"]) >= .65 and
                   x0 <= b[0]+10 and y0 <= b[1]+10 and x1 >= b[2]-10 and y1 >= b[3]-10]
        own = [a for a in evidence["anchors"] if x0 <= a["center"][0] <= x1 and y0 <= a["center"][1] <= y1]
        ids = {v for a in own for v in a["ids"]}
        if len(ids) > 1:
            issues.append(f"region {index}: multiple document identities in one region")
        if ids & used_ids:
            issues.append(f"region {index}: repeated identity anchor")
        used_ids.update(ids)
        frame_index = borders[0][0] if borders else None
        if frame_index is not None and frame_index in used_frames:
            issues.append(f"region {index}: shared document boundary")
        if frame_index is not None:
            used_frames.add(frame_index)
        if len(regions) > 1 and not borders and not (len(ids) == 1 and any(a["amount"] for a in own)):
            issues.append(f"region {index}: no independent complete-document evidence")
    return issues


def single_document(evidence):
    ocr_ids = {v for a in evidence["anchors"] if a.get("source") != "qr" for v in a["ids"]}
    identities = [v for a in evidence["anchors"] for v in a["ids"] if a.get("source") != "qr" or v not in ocr_ids]
    if len(identities) != len(set(identities)) or len(set(identities)) > 1:
        return None
    if len(evidence["frames"]) == 1:
        return evidence["frames"][0]
    ids = {v for a in evidence["anchors"] for v in a["ids"]}
    if not evidence["frames"] and len(ids) == 1 and any(a["amount"] for a in evidence["anchors"]):
        return [0, 0, 1000, 1000]
    return None


def enhancement(source, lines, target):
    """One deterministic copy only for measured text contrast or reliable skew."""
    import cv2
    import numpy as np
    with Image.open(source) as image:
        gray = np.asarray(image.convert("L"))
    h, w = gray.shape
    angles, contrasts, sharpness = [], [], []
    for line in lines or []:
        if line.get("score", line.get("confidence", 1)) < .8 or len(line.get("text", "").strip()) < 3:
            continue
        p = line["box"]
        if len(p) != 4:
            continue
        dx, dy = p[1][0]-p[0][0], p[1][1]-p[0][1]
        if dx > 20:
            angles.append(math.degrees(math.atan2(dy, dx)))
        x0, y0 = max(0,int(min(v[0] for v in p))), max(0,int(min(v[1] for v in p)))
        x1, y1 = min(w,int(max(v[0] for v in p))), min(h,int(max(v[1] for v in p)))
        roi = gray[y0:y1, x0:x1]
        if roi.size:
            contrasts.append(float(np.percentile(roi,95)-np.percentile(roi,5)))
            sharpness.append(float(cv2.Laplacian(roi, cv2.CV_64F).var()))
    angle = float(np.median(angles)) if len(angles) >= 3 else 0
    skew = .5 <= abs(angle) <= 7 and sum(abs(a-angle) < .5 for a in angles) >= 3
    low = bool(contrasts) and float(np.median(contrasts)) < 45
    blur = bool(sharpness) and float(np.median(sharpness)) < 20
    info = {"source_size": [w,h], "low_text_contrast": low, "skew_degrees": angle if skew else 0,
            "blur_warning": blur, "matrix": [[1,0,0],[0,1,0]], "applied": False}
    if target is None or not low and not skew:
        return None, info
    if low:
        gray = cv2.createCLAHE(clipLimit=2, tileGridSize=(8,8)).apply(gray)
    if skew:
        matrix = cv2.getRotationMatrix2D((w/2,h/2), angle, 1)
        gray = cv2.warpAffine(gray, matrix, (w,h), borderValue=255)
        info["matrix"] = matrix.tolist()
    Image.fromarray(gray).save(target)
    info.update(applied=True, path=str(target))
    return str(target), info
