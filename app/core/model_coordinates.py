"""Explicit, calibrated model frames. Unknown geometry never authorizes a crop."""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import tempfile
from pathlib import Path
from PIL import Image
from app import config
from app.core import geometry, invocation_trace

PROFILE_VERSION = 1
CONTRACT_VERSION = 3
INPUT_PREPROCESSING = {"version":1,"encoding":"PNG","color_mode":"RGB","resampling":"LANCZOS",
                       "upscale":False,"rotation":0,"max_edge_policy":"caller_limit"}

# Checked against the server's pinned runner and upstream image implementation.
# Adding a backend/version requires a source review, never a fit to model boxes.
CHECKED_PROCESSORS = {"0.35.0": {
    "kind": "qwen_smart_resize", "server_version": "0.35.0", "factor": 28,
    "min_pixels": 802816, "max_pixels": 3211264, "padding_mode": "ceil_center",
    "source_url": "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/mtmd-image.cpp",
    "source_sha256": "7dc760e4f85e38e11d11268ae412c3b6c0a32389c6c584ca34dd7b4a679f950b",
    "provenance": {
        "https://raw.githubusercontent.com/ollama/ollama/v0.35.0/llm/llama_server.go": "10777cca3193c12b4cfcc90f5cfd88085c298f7a2c2c7333b0b95bb6e545d391",
        "https://raw.githubusercontent.com/ollama/ollama/v0.35.0/LLAMA_CPP_VERSION": "55b8f69cefcfc8fb7ddec8b5908e2e015d35f712c42b8b13fcf7f4417b3410b2",
        "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/clip.cpp": "f50b204fa1bd4138c53d041663bbaf1472bab3af3407b95a8023429eb14e51ce",
        "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/clip-model.h": "f4df467f21eb5ac4954010a3be30f26586ebe7fa77f6000e65a943215be011e2",
        "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/mtmd-image.cpp": "7dc760e4f85e38e11d11268ae412c3b6c0a32389c6c584ca34dd7b4a679f950b",
    },
}}


def validate_processor(processor):
    checked = CHECKED_PROCESSORS.get(processor.get("server_version"))
    if not checked or any(processor.get(key) != value for key, value in checked.items()):
        raise ValueError("visual processor differs from checked version/source parameters")
    return processor


def _float32(value):
    return struct.unpack("f", struct.pack("f", value))[0]


def profile_path():
    return config.DATA_DIR / "model_coordinate_profiles.json"


def registry():
    try:
        value = json.loads(profile_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("version") == PROFILE_VERSION else {"version": PROFILE_VERSION, "profiles": []}
    except (OSError, ValueError):
        return {"version": PROFILE_VERSION, "profiles": []}


def registry_digest():
    return hashlib.sha256(json.dumps(registry(), sort_keys=True).encode()).hexdigest()


def ollama_identity(model):
    """Metadata only; does not load or invoke a model."""
    import requests
    from app.services import sysconfig
    url = (sysconfig.get_section("llm").get("ollama_base_url") or config.OLLAMA_BASE_URL).rstrip("/")
    version = requests.get(url + "/api/version", timeout=4)
    version.raise_for_status()
    tags = requests.get(url + "/api/tags", timeout=4)
    tags.raise_for_status()
    names = {model, model + ":latest"}
    found = next((m for m in tags.json().get("models", []) if m.get("name") in names or m.get("model") in names), None)
    if not found or not found.get("digest"):
        raise ValueError("model digest unavailable")
    show = requests.post(url + "/api/show", json={"model":model}, timeout=4)
    show.raise_for_status()
    metadata = show.json().get("model_info") or {}
    if (metadata.get("general.architecture") != "qwen25vl" or metadata.get("qwen25vl.vision.patch_size") != 14 or
            metadata.get("qwen25vl.vision.spatial_merge_size") != 2):
        raise ValueError("model vision architecture does not match checked processor")
    return {"provider": "ollama", "backend_url": url, "server_version": version.json()["version"],
            "model": model, "model_digest": found["digest"]}


def smart_resize(size, processor):
    """A version-pinned Qwen processor description, never inferred from boxes."""
    width, height = size
    factor = processor["factor"]
    minimum, maximum = processor["min_pixels"], processor["max_pixels"]
    if type(factor) is not int or factor <= 0 or min(width, height) <= 0 or not 0 < minimum <= maximum:
        raise ValueError("invalid or unsupported processor dimensions")
    # Go math.Round and Python round differ at half integers.
    rounded = lambda n: max(factor, math.floor(_float32(n / factor) + .5) * factor)
    rw, rh = rounded(width), rounded(height)
    if rw * rh > maximum:
        ratio = _float32(math.sqrt(_float32(_float32(width * height) / maximum)))
        rw, rh = [max(factor, math.floor(_float32(_float32(n / ratio) / factor)) * factor) for n in (width, height)]
    elif rw * rh < minimum:
        ratio = _float32(math.sqrt(_float32(minimum / _float32(width * height))))
        rw, rh = [math.ceil(_float32(_float32(n * ratio) / factor)) * factor for n in (width, height)]
    if min(rw, rh) <= 0:
        raise ValueError("processor produced empty image")
    return [rw, rh]


def visual_frame(input_size, processor):
    visual = smart_resize(input_size, processor) if processor["kind"] == "qwen_smart_resize" else list(input_size)
    if processor.get("padding_mode") == "ceil_center":
        scale = min(_float32(visual[i] / input_size[i]) for i in (0, 1))
        content = [min(math.ceil(_float32(input_size[i] * scale)), visual[i]) for i in (0, 1)]
        offset = [(visual[i] - content[i]) // 2 for i in (0, 1)]
    elif processor.get("padding_mode", "none") == "none":
        content, offset = visual, [0, 0]
    else:
        raise ValueError("unsupported processor padding")
    return {"visual_size": visual, "content_size": content,
            "padding": [*offset, visual[0]-content[0]-offset[0], visual[1]-content[1]-offset[1]],
            "transform": [[1000/content[0], 0, -offset[0]*1000/content[0]], [0, 1000/content[1], -offset[1]*1000/content[1]]]}


def from_profile(profile, input_size, *, identity=None):
    """Profiles must prove both processor provenance and all calibration targets."""
    base = {"contract_version": CONTRACT_VERSION, "verified": False, "input_size": list(input_size),
            "system_unit": "normalized_1000", "profile_id": profile.get("id") if profile else None}
    if not profile:
        return {**base, "reason": "no verified coordinate profile"}
    if identity and any(profile.get(k) != identity.get(k) for k in ("provider", "backend_url", "server_version", "model", "model_digest")):
        return {**base, "reason": "coordinate profile runtime mismatch"}
    proof = profile.get("calibration") or {}
    processor = profile.get("processor") or {}
    if not (profile.get("verified") is True and proof.get("version") == PROFILE_VERSION and
            proof.get("targets", 0) >= 12 and proof.get("min_iou", 0) >= .9 and
            profile.get("input_preprocessing") == INPUT_PREPROCESSING and
            processor.get("source_sha256") and processor.get("source_url") and
            processor.get("server_version") == profile.get("server_version")):
        return {**base, "reason": "coordinate profile lacks processor/calibration proof"}
    try:
        validate_processor(processor)
        if processor.get("kind") not in ("qwen_smart_resize", "identity"):
            raise ValueError("unsupported visual processor")
        unit = profile["unit"]
        if unit not in ("pixels", "normalized_1000"):
            raise ValueError("unsupported model coordinate unit")
        frame = visual_frame(input_size, processor)
        return {**base, "verified": True, "model_unit": unit, **frame,
                "processor": processor, "calibration_version": proof["version"],
                "model_digest": profile.get("model_digest"), "server_version": profile.get("server_version"),
                "normalized_frame": "visual_image"}
    except (KeyError, TypeError, ValueError) as exc:
        return {**base, "reason": str(exc)}


def for_image(image_path, setting, *, max_edge=1180):
    from app.core.settings_store import parse_setting
    provider, model = parse_setting(setting)
    with Image.open(image_path) as image:
        source_size = image.size
        image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        size = image.size
        from io import BytesIO
        sent = BytesIO()
        image.save(sent, format="PNG")
    sent_path = Path(image_path).with_name(Path(image_path).stem + "_model_input.png")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=sent_path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(sent.getvalue())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, sent_path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    profiles = [p for p in registry().get("profiles", []) if p.get("provider") == provider and p.get("model") == model]
    context = from_profile(None, size)
    if profiles or provider == "ollama" and model.startswith("qwen2.5vl"):
        try:
            if provider != "ollama":
                raise ValueError("no runtime identity verifier for this backend")
            identity = ollama_identity(model)
            profile = next((p for p in profiles if all(p.get(k) == identity.get(k) for k in identity)), None)
            context = from_profile(profile, size, identity=identity)
            processor = CHECKED_PROCESSORS.get(identity["server_version"])
            if processor:
                validate_processor(processor)
                context.update(**visual_frame(size, processor), processor=processor,
                               processor_source_checked=True)
            context.update(backend_url=identity["backend_url"], server_version=identity["server_version"],
                           model_digest=identity["model_digest"])
        except Exception as exc:
            context["reason"] = "runtime geometry unavailable: " + str(exc)[:180]
    context.update(provider=provider, model=model, source_size=list(source_size), registry_sha256=registry_digest(),
                   input_path=str(sent_path),
                   source_sha256=hashlib.sha256(Path(image_path).read_bytes()).hexdigest(), sent_sha256=hashlib.sha256(sent.getvalue()).hexdigest(),
                   preprocessing={**INPUT_PREPROCESSING,"max_edge":max_edge,"padding":[0,0,0,0],
                                  "source_to_input":[[size[0]/source_size[0],0,0],[0,size[1]/source_size[1],0]]})
    invocation_trace.add({"kind": "model_coordinate_context", **context})
    return context


def convert(box, context):
    if not context or not context.get("verified"):
        raise ValueError("model coordinates are unverified")
    if not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box):
        raise ValueError("model box must contain four integer coordinates")
    w, h = context["visual_size"]
    if context["model_unit"] == "normalized_1000":
        geometry.validate_box(box)
        box = [box[0]*w/1000, box[1]*h/1000, box[2]*w/1000, box[3]*h/1000]
    if not (0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h):
        raise ValueError("model pixel box reversed, empty or outside verified visual frame")
    transform = context.get("transform", [[1000/w,0,0],[0,1000/h,0]])
    converted = [round(box[0]*transform[0][0]+transform[0][2]), round(box[1]*transform[1][1]+transform[1][2]),
                 round(box[2]*transform[0][0]+transform[0][2]), round(box[3]*transform[1][1]+transform[1][2])]
    return geometry.validate_box(converted)


def convert_fields(raw, allowed, context):
    valid, issues = {}, []
    if not isinstance(raw, dict):
        return {}, [{"field": "boxes", "raw": raw, "reason": "boxes must be an object"}]
    for key, box in raw.items():
        try:
            if key not in allowed:
                raise ValueError("unknown field box")
            valid[key] = convert(box, context)
        except (ValueError, TypeError, KeyError) as exc:
            issues.append({"field": key, "raw": box, "reason": str(exc)})
    return valid, issues


def box_schema(context):
    if not context or not context.get("verified"):
        return {"type": "object", "properties": {}, "additionalProperties": False}
    max_value = max(context["visual_size"]) if context["model_unit"] == "pixels" else 1000
    return {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": max_value}, "minItems": 4, "maxItems": 4}


def instructions(context):
    if not context or not context.get("verified"):
        return "模型定位座標未校準；boxes 必須為 {}。欄位內容仍照契約擷取。"
    if context["model_unit"] == "pixels":
        w, h = context["visual_size"]
        return f"定位框為視覺處理後 {w}×{h} 圖片的絕對像素整數 [x1,y1,x2,y2]，未知省略；不可使用0–1000比例。"
    return "定位框為視覺處理後圖片（含補邊）的0–1000整數 [x1,y1,x2,y2]，未知省略。"
