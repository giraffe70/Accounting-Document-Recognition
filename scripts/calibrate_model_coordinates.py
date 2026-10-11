"""Calibrate native box units against synthetic rectangles and a checked processor."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

import requests
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import model_coordinates as coordinates  # noqa: E402
from app.core.geometry import iou, validate_box  # noqa: E402

MIN_IOU = .9
REPEATS = 2
TARGETS = (
    (640, 480, (24, 28, 218, 126), "red"),
    (800, 400, (540, 36, 774, 172), "blue"),
    (400, 800, (36, 574, 184, 770), "green"),
    (713, 503, (524, 362, 699, 479), "yellow"),
    (503, 713, (20, 523, 168, 690), "magenta"),
    (1024, 768, (738, 29, 1001, 196), "cyan"),
)
COLORS = {"red": (235, 35, 35), "blue": (35, 80, 235), "green": (30, 175, 60),
          "yellow": (245, 210, 25), "magenta": (210, 35, 190), "cyan": (20, 190, 205)}


class CalibrationError(ValueError):
    pass


class ChatCallError(RuntimeError):
    def __init__(self, message: str, evidence: dict):
        super().__init__(message)
        self.evidence = evidence


def make_synthetic(index: int) -> tuple[Image.Image, list[int], str]:
    width, height, rect, color_name = TARGETS[index]
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle(rect, fill=COLORS[color_name])
    draw.text((8, 8), f"target_{index + 1}", fill="black")
    return image, list(rect), f"target_{index + 1}"


def validate_processor_spec(spec: dict, identity: dict, *, get=requests.get) -> dict:
    """Apply the core's reviewed allowlist, then verify every pinned source blob."""
    if not isinstance(spec, dict) or spec.get("version") != 1:
        raise CalibrationError("unsupported processor spec version")
    for key in ("provider", "backend_url", "server_version", "model", "model_digest"):
        if spec.get(key) != identity.get(key):
            raise CalibrationError(f"processor spec runtime mismatch: {key}")
    processor = spec.get("processor")
    if not isinstance(processor, dict):
        raise CalibrationError("processor description is missing")
    validator = getattr(coordinates, "validate_processor", None)
    if not callable(validator):
        raise CalibrationError("core has no checked processor allowlist")
    try:
        accepted = validator(processor)
    except Exception as exc:
        raise CalibrationError(f"processor is not in the checked allowlist: {exc}") from exc
    if accepted is False:
        raise CalibrationError("processor is not in the checked allowlist")
    if isinstance(accepted, dict):
        processor = accepted
    provenance = processor.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise CalibrationError("processor provenance map is missing")
    for url, expected_digest in provenance.items():
        if not isinstance(url, str) or not isinstance(expected_digest, str):
            raise CalibrationError("processor provenance entry is invalid")
        response = get(url, timeout=20)
        response.raise_for_status()
        content = getattr(response, "content", None)
        if not isinstance(content, bytes):
            raise CalibrationError(f"processor source did not return bytes: {url}")
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_digest:
            raise CalibrationError(f"processor source SHA-256 mismatch: {url}")
    if processor.get("source_url") not in provenance:
        raise CalibrationError("primary processor source is absent from provenance map")
    if provenance[processor["source_url"]] != processor.get("source_sha256"):
        raise CalibrationError("primary processor source hash does not match provenance")
    return processor


def ollama_identity_at(base_url: str, model: str, *, get=requests.get, post=requests.post) -> dict:
    base = base_url.rstrip("/")
    version_response = get(base + "/api/version", timeout=5)
    version_response.raise_for_status()
    tags_response = get(base + "/api/tags", timeout=5)
    tags_response.raise_for_status()
    version = version_response.json().get("version")
    matches = [item for item in tags_response.json().get("models", [])
               if item.get("name") in {model, model + ":latest"} or item.get("model") in {model, model + ":latest"}]
    if not version or len(matches) != 1 or not matches[0].get("digest"):
        raise CalibrationError("could not establish exact Ollama server/model identity")
    show_response = post(base + "/api/show", json={"model": model}, timeout=5)
    show_response.raise_for_status()
    model_info = show_response.json().get("model_info") or {}
    if (model_info.get("general.architecture") != "qwen25vl" or
            model_info.get("qwen25vl.vision.patch_size") != 14 or
            model_info.get("qwen25vl.vision.spatial_merge_size") != 2):
        raise CalibrationError("model vision architecture does not match checked processor")
    return {"provider": "ollama", "backend_url": base, "server_version": version,
            "model": model, "model_digest": matches[0]["digest"]}


def ollama_chat(base_url: str, model: str, image: Image.Image, prompt: str, *, post=requests.post) -> dict:
    from io import BytesIO
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = {"model": model, "stream": False, "format": {
        "type": "object", "properties": {"targets": {"type": "object", "properties": {
            f"target_{i + 1}": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4}
            for i in range(len(TARGETS))}, "additionalProperties": False}},
        "required": ["targets"], "additionalProperties": False},
        "options": {"num_predict": 512, "temperature": 0},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(buffer.getvalue()).decode("ascii")]}]}
    response = post(base_url.rstrip("/") + "/api/chat", json=payload, timeout=60)
    raw_text = getattr(response, "text", "")
    try:
        raw_reply = response.json()
    except Exception:
        raw_reply = None
    backend = _backend_metrics(raw_reply)
    evidence = {"http_status": getattr(response, "status_code", None), "raw_reply": raw_reply,
                "raw_text": raw_text, "backend": backend}
    try:
        response.raise_for_status()
    except Exception as exc:
        raise ChatCallError(f"Ollama HTTP error: {exc}", evidence) from exc
    if not isinstance(raw_reply, dict):
        raise ChatCallError("Ollama returned invalid JSON", evidence)
    content = raw_reply.get("message", {}).get("content")
    if not isinstance(content, str):
        raise ChatCallError("Ollama response has no message content", evidence)
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise ChatCallError("model response is not valid JSON", evidence) from exc
    return {"parsed": parsed, **evidence}


def _backend_metrics(raw_reply) -> dict:
    if not isinstance(raw_reply, dict):
        return {}
    keys = ("prompt_eval_count", "eval_count", "done_reason", "total_duration", "load_duration",
            "prompt_eval_duration", "eval_duration")
    return {key: raw_reply[key] for key in keys if key in raw_reply}


def _to_system_box(raw_box, unit: str, frame: dict) -> list[int]:
    context = {"verified": True, "model_unit": unit, **frame}
    try:
        return coordinates.convert(raw_box, context)
    except (ValueError, TypeError, KeyError) as exc:
        raise CalibrationError(str(exc)) from exc


def score_responses(responses: list[tuple[int, dict]], processor: dict) -> dict:
    scores: dict[str, list[float]] = {"pixels": [], "normalized_1000": []}
    observations = []
    for index, response in responses:
        image, expected, label = make_synthetic(index)
        frame = coordinates.visual_frame(image.size, processor)
        targets = response.get("targets") if isinstance(response, dict) else None
        raw = targets.get(label) if isinstance(targets, dict) else None
        expected_system = validate_box([
            round(expected[0] * 1000 / image.width), round(expected[1] * 1000 / image.height),
            round(expected[2] * 1000 / image.width), round(expected[3] * 1000 / image.height)])
        observation = {"target": label, "input_size": list(image.size),
                       "visual_size": list(frame["visual_size"]),
                       "content_size": list(frame["content_size"]), "padding": list(frame["padding"]),
                       "raw_box": raw, "iou": {}}
        for unit in scores:
            try:
                predicted = _to_system_box(raw, unit, frame)
                value = iou(predicted, expected_system)
            except (CalibrationError, ValueError, TypeError, KeyError, IndexError):
                value = 0.0
            scores[unit].append(value)
            observation["iou"][unit] = value
        observations.append(observation)
    per_unit = {unit: {"count": len(values), "min_iou": min(values) if values else 0.0,
                       "ious": values} for unit, values in scores.items()}
    passing = [unit for unit, details in per_unit.items()
               if details["count"] == 12 and details["min_iou"] >= MIN_IOU]
    unit = passing[0] if len(passing) == 1 else None
    return {"unit": unit, "minimum_iou": per_unit[unit]["min_iou"] if unit else 0.0,
            "per_unit_iou": per_unit, "observations": observations,
            "reason": None if unit else "coordinate unit is ambiguous or at least one target IoU is below 0.9"}


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _call_payload(value) -> dict:
    if isinstance(value, dict) and "parsed" in value:
        return {"parsed": value["parsed"], "raw_reply": value.get("raw_reply"),
                "raw_text": value.get("raw_text", ""), "http_status": value.get("http_status"),
                "backend": value.get("backend") or _backend_metrics(value.get("raw_reply"))}
    return {"parsed": value, "raw_reply": value, "raw_text": "", "http_status": None,
            "backend": _backend_metrics(value)}


def run_calibration(*, output_dir: Path, spec_path: Path, model: str, base_url: str,
                    identity_fn=ollama_identity_at, chat_fn=ollama_chat, get=requests.get,
                    dry_run: bool = False) -> dict:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise CalibrationError("output directory must be new and nonexistent")
    output_dir.mkdir(parents=True)
    report_path = output_dir / "calibration_report.json"
    call_dir = output_dir / "calls"
    call_dir.mkdir()
    report = {"version":coordinates.PROFILE_VERSION,"coordinate_contract_version":coordinates.CONTRACT_VERSION,
              "input_preprocessing":coordinates.INPUT_PREPROCESSING,
              "verified": False, "calls": 0, "identity": None, "processor": None,
              "call_evidence": [], "observations": [], "per_unit_iou": {}, "reason": None}
    _write_json(report_path, report)
    try:
        identity = identity_fn(base_url, model)
        report["identity"] = identity
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        processor = validate_processor_spec(spec, identity, get=get)
        report["processor"] = processor
    except Exception as exc:
        report["reason"] = str(exc)
        _write_json(report_path, report)
        return report
    _write_json(report_path, report)

    for index in range(len(TARGETS)):
        image, _, label = make_synthetic(index)
        image.save(output_dir / f"{label}.png")

    if dry_run:
        report["dry_run"] = True
        report["reason"] = "dry run completed; no model evidence collected"
        _write_json(report_path, report)
        return report

    responses = []
    for index in range(len(TARGETS)):
        image, _, label = make_synthetic(index)
        prompt = ("Return JSON only: {\"targets\": {\"target_N\": [x1,y1,x2,y2]}}. "
                  f"Find the solid {TARGETS[index][3]} rectangle labeled {label}. "
                  "Coordinates must describe the full rectangle in the model's native output coordinate system.")
        for repeat in range(REPEATS):
            if report["calls"] >= 12:
                report["reason"] = "hard model call limit reached"
                _write_json(report_path, report)
                return report
            call_number = report["calls"] + 1
            record = {"call": call_number, "target": label, "repeat": repeat + 1, "status": "running"}
            report["calls"] = call_number
            report["call_evidence"].append(record)
            _write_json(call_dir / f"call-{call_number:02d}.json", record)
            _write_json(report_path, report)
            try:
                result = chat_fn(base_url, model, image, prompt)
                evidence = _call_payload(result)
                record.update(evidence)
                record["status"] = "complete"
                responses.append((index, evidence["parsed"]))
            except Exception as exc:
                if isinstance(exc, ChatCallError):
                    record.update(exc.evidence)
                record["status"] = "failed"
                record["error"] = str(exc)
                report["reason"] = str(exc)
                _write_json(call_dir / f"call-{call_number:02d}.json", record)
                _write_json(report_path, report)
                return report
            _write_json(call_dir / f"call-{call_number:02d}.json", record)
            _write_json(report_path, report)

    try:
        diagnostic = score_responses(responses, processor)
    except Exception as exc:
        report["reason"] = f"coordinate scoring failed: {exc}"
        _write_json(report_path, report)
        return report
    report["observations"] = diagnostic["observations"]
    report["per_unit_iou"] = diagnostic["per_unit_iou"]
    report["reason"] = diagnostic["reason"]
    if not diagnostic["unit"]:
        _write_json(report_path, report)
        return report

    # A profile is committed only if the Ollama identity has remained unchanged.
    try:
        final_identity = identity_fn(base_url, model)
    except Exception as exc:
        report["reason"] = f"final runtime identity check failed: {exc}"
        _write_json(report_path, report)
        return report
    if final_identity != identity:
        report["final_identity"] = final_identity
        report["reason"] = "runtime identity changed during calibration"
        _write_json(report_path, report)
        return report

    profile = {**identity,
               "id": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16],
               "unit": diagnostic["unit"], "processor": processor,"input_preprocessing":coordinates.INPUT_PREPROCESSING,
               "calibration": {"version": 1, "targets": len(diagnostic["observations"]),
                               "min_iou": diagnostic["minimum_iou"],
                               "per_unit_iou": diagnostic["per_unit_iou"],
                               "observations": diagnostic["observations"]}, "verified": True}
    report["verified"] = True
    report["reason"] = None
    report["profile"] = profile
    _write_json(report_path, report)
    _write_json(output_dir / "model_coordinate_profile.json", profile)
    registry_path = coordinates.profile_path()
    registry = coordinates.registry()
    registry.setdefault("profiles", [])
    registry["profiles"] = [p for p in registry["profiles"]
                            if not all(p.get(k) == identity.get(k) for k in identity)] + [profile]
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = registry_path.with_suffix(registry_path.suffix + ".tmp")
    _write_json(temp_path, registry)
    temp_path.replace(registry_path)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processor-spec", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="new directory for reports, calls, and synthetic images")
    parser.add_argument("--dry-run", action="store_true", help="validate source/runtime; do not call the model")
    args = parser.parse_args(argv)
    report = run_calibration(output_dir=args.output_dir, spec_path=args.processor_spec,
                             model=args.model, base_url=args.base_url, dry_run=args.dry_run)
    summary = {"verified": report.get("verified"), "calls": report.get("calls"),
               "reason": report.get("reason"),
               "per_unit_min_iou": {unit: values.get("min_iou")
                                    for unit, values in report.get("per_unit_iou", {}).items()}}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if report.get("verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
