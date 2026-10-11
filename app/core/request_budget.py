"""Offline tokenizer accounting and one-file, pre-inference image budgeting.

Preparation may fetch public tokenizer assets and backend metadata. Runtime never
downloads a vocabulary and never learns a coordinate scale from model responses.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from PIL import Image
from app import config
from app.core import invocation_trace, model_coordinates

VERSION = 1
REVISION = "cc594898137f460bfe9f0759e9844b3ce807cfb5"
REPOSITORY = "Qwen/Qwen2.5-VL-7B-Instruct"
TOKENIZERS_VERSION = "0.22.2"
SAFETY = 512
EDGES = (2048, 1800, 1440, 1180, 1024, 896)
# Exact template reviewed in the local Ollama model, not the model-name heuristic.
QWEN_TEMPLATE = '''{{- if .System -}}
<|im_start|>system
{{ .System }}<|im_end|>
{{- end -}}
{{- range $i, $_ := .Messages }}
{{- $last := eq (len (slice $.Messages $i)) 1 -}}
{{- if eq .Role "user" }}
<|im_start|>user
{{ .Content }}<|im_end|>
{{- else if eq .Role "assistant" }}
<|im_start|>assistant
{{ if .Content }}{{ .Content }}{{ if not $last }}<|im_end|>
{{- else -}}<|im_end|>{{- end -}}
{{- end -}}
{{- end -}}
{{- if and (ne .Role "assistant") $last }}
<|im_start|>assistant
{{ end -}}
{{- end }}'''
_tokenizers = {}


class BudgetError(ValueError):
    def __init__(self, code, message, details=None):
        self.code, self.details = code, details or {}
        super().__init__(f"{code}: {message}")


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def asset_directory():
    return Path(os.environ["PDFSCAN_TOKENIZER_DIR"]) if os.environ.get("PDFSCAN_TOKENIZER_DIR") else config.DATA_DIR / "token_budget"


def registry_digest():
    path = asset_directory() / "manifest.json"
    return _sha(path.read_bytes()) if path.is_file() else None


def _metadata(model, backend_url, *, verbose=False):
    import requests
    url = backend_url.rstrip("/")
    def read(method, endpoint, **kwargs):
        response = getattr(requests, method)(url + endpoint, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()
    version = read("get", "/api/version")["version"]
    names = {model, model + ":latest"}
    tag = next((m for m in read("get", "/api/tags")["models"] if m.get("name") in names or m.get("model") in names), None)
    if not tag or not tag.get("digest"):
        raise BudgetError("budget_profile_not_ready", "model digest unavailable")
    show = read("post", "/api/show", json={"model": model, "verbose": verbose})
    return {"provider":"ollama", "backend_url":url, "server_version":version, "model":model,
            "model_digest":tag["digest"]}, show


def verify_vocabulary(tokenizer, metadata):
    tokens = metadata.get("tokenizer.ggml.tokens") or []
    vocab = {**tokenizer["model"]["vocab"], **{t["content"]:t["id"] for t in tokenizer.get("added_tokens", [])}}
    if metadata.get("tokenizer.ggml.pre") != "qwen2" or any(i >= len(tokens) or tokens[i] != t for t, i in vocab.items()):
        raise BudgetError("budget_profile_not_ready", "backend token IDs differ from pinned vocabulary")
    merges = tokenizer["model"]["merges"]
    merges = [" ".join(m) if isinstance(m, list) else m for m in merges]
    if merges != metadata.get("tokenizer.ggml.merges"):
        raise BudgetError("budget_profile_not_ready", "backend merge rules differ from pinned vocabulary")
    return {"verified_ids":len(vocab), "backend_ids":len(tokens), "merges":len(merges)}


def prepare_profile(model, backend_url=None, asset_dir=None):
    """Explicit preparation only: two tokenizer files, metadata, hashes, no weights."""
    import requests
    import tokenizers
    from app.services import sysconfig
    if tokenizers.__version__ != TOKENIZERS_VERSION:
        raise BudgetError("budget_profile_not_ready", "tokenizers version mismatch")
    folder = Path(asset_dir) if asset_dir else asset_directory()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    files = {}
    for name in ("tokenizer.json", "tokenizer_config.json"):
        path = folder / name
        cached = path.read_bytes() if path.is_file() else b""
        if existing.get("repository") == REPOSITORY and existing.get("revision") == REVISION and (existing.get("files") or {}).get(name) == _sha(cached):
            content = cached
        else:
            response = requests.get(f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}", timeout=120)
            response.raise_for_status()
            content = response.content
        json.loads(content)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        files[name] = _sha(content)
    url = backend_url or sysconfig.get_section("llm").get("ollama_base_url") or config.OLLAMA_BASE_URL
    identity, show = _metadata(model, url, verbose=True)
    processor = model_coordinates.CHECKED_PROCESSORS.get(identity["server_version"])
    info = show.get("model_info") or {}
    if not processor or info.get("general.architecture") != "qwen25vl" or info.get("qwen25vl.vision.patch_size") != 14 or info.get("qwen25vl.vision.spatial_merge_size") != 2:
        raise BudgetError("budget_profile_not_ready", "visual processor is not source-verified")
    template = show.get("template", "")
    if template.strip() != QWEN_TEMPLATE.strip():
        raise BudgetError("budget_profile_not_ready", "chat template not supported by reviewed renderer")
    proof = verify_vocabulary(json.loads((folder / "tokenizer.json").read_text(encoding="utf-8")), info)
    profile = {**identity, "version":VERSION, "tokenizers_version":TOKENIZERS_VERSION, "repository":REPOSITORY,
               "revision":REVISION, "files":files, "vocabulary_proof":proof, "template":template,
               "template_sha256":_sha(template), "default_system":show.get("system", ""),
               "processor":processor, "input_preprocessing":model_coordinates.INPUT_PREPROCESSING,
               "template_extra_tokens":64, "image_special_tokens":16}
    profile["profile_id"] = _sha(json.dumps(profile, sort_keys=True))
    invocation_trace.atomic_json(folder / "manifest.json", profile)
    return profile


def runtime_profile(chat):
    """Load local assets; live metadata checks cannot silently bless changed models."""
    underlying = getattr(chat, "bound", chat)
    folder = asset_directory()
    try:
        import tokenizers
        profile = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if "ollama" not in type(underlying).__module__.lower():
            raise ValueError("no reviewed tokenizer/processor profile for this backend")
        if (profile["version"] != VERSION or tokenizers.__version__ != TOKENIZERS_VERSION or profile["tokenizers_version"] != TOKENIZERS_VERSION or profile["revision"] != REVISION or
                profile["repository"] != REPOSITORY or set(profile["files"]) != {"tokenizer.json","tokenizer_config.json"}):
            raise ValueError("profile/tokenizer version mismatch")
        if profile.get("profile_id") != _sha(json.dumps({k:v for k,v in profile.items() if k!="profile_id"}, sort_keys=True)):
            raise ValueError("profile manifest hash mismatch")
        if profile.get("input_preprocessing") != model_coordinates.INPUT_PREPROCESSING or profile.get("template", "").strip() != QWEN_TEMPLATE.strip():
            raise ValueError("profile preprocessing/template renderer mismatch")
        for name, digest in profile["files"].items():
            if name not in ("tokenizer.json", "tokenizer_config.json") or _sha((folder / name).read_bytes()) != digest:
                raise ValueError("tokenizer asset hash mismatch")
        model = getattr(underlying, "model", None)
        url = getattr(underlying, "base_url", None) or config.OLLAMA_BASE_URL
        identity, show = _metadata(model, url)
        if any(profile.get(k) != v for k, v in identity.items()) or _sha(show.get("template", "")) != profile["template_sha256"] or show.get("system", "") != profile["default_system"]:
            raise ValueError("backend identity or chat template changed; prepare again")
        model_coordinates.validate_processor(profile["processor"])
        key = profile["files"]["tokenizer.json"]
        if key not in _tokenizers:
            _tokenizers[key] = tokenizers.Tokenizer.from_file(str(folder / "tokenizer.json"))
        return profile, _tokenizers[key]
    except BudgetError:
        raise
    except Exception as exc:
        raise BudgetError("budget_profile_not_ready", str(exc)) from exc


def status():
    try:
        from app.core.model_factory import get_chat_model
        profile, _ = runtime_profile(get_chat_model("ollama", "qwen2.5vl:7b"))
        return {"ready":True, "profile_id":profile["profile_id"], "coordinate_boxes_enabled":False}
    except Exception as exc:
        return {"ready":False, "reason":str(exc)}


def image_part(context):
    """Internal deferred image; the SDK receives only the final data URI."""
    return {"type":"image_url", "image_url":{"url":""}, "_budget_image":context}


def _text(message):
    value = message.get("content", "")
    return "".join(p.get("text", "") for p in value if p.get("type") == "text") if isinstance(value, list) else str(value)


def count(messages, schema, sizes, profile, tokenizer, output_limit, context):
    system = "\n".join(_text(m) for m in messages if m["role"] == "system") or profile.get("default_system", "")
    rendered = f"<|im_start|>system\n{system}<|im_end|>\n" if system else ""
    for message in messages:
        if message["role"] == "system":
            continue
        if message["role"] not in ("user", "assistant"):
            raise BudgetError("budget_profile_not_ready", "unsupported chat role")
        rendered += f"<|im_start|>{message['role']}\n{_text(message)}<|im_end|>\n"
    rendered += "<|im_start|>assistant\n"
    text = len(tokenizer.encode(rendered, add_special_tokens=False).ids)
    schema_tokens = len(tokenizer.encode(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), add_special_tokens=False).ids) if schema else 0
    frames = [model_coordinates.visual_frame(size, profile["processor"]) for size in sizes]
    visual = sum(f["visual_size"][0] * f["visual_size"][1] // profile["processor"]["factor"]**2 for f in frames)
    specials = profile.get("template_extra_tokens", 64) + len(sizes)*profile.get("image_special_tokens", 16)
    total = text + schema_tokens + visual + specials
    return {"version":VERSION, "profile_id":profile["profile_id"], "text_tokens":text, "schema_tokens":schema_tokens,
            "visual_tokens":visual, "template_special_reserve":specials, "estimated_input_tokens":total,
            "output_limit":output_limit, "safety_tokens":SAFETY, "context_limit":context,
            "fits":total + output_limit + SAFETY <= context, "input_sizes":[list(s) for s in sizes],
            "visual_sizes":[f["visual_size"] for f in frames]}


def preflight(chat, messages, schema, output_limit, *, task="extract"):
    underlying = getattr(chat, "bound", chat)
    # Offline fakes have no SDK model. Real adapters must always resolve a profile.
    if not any(s in type(underlying).__module__.lower() for s in ("ollama", "openai", "anthropic", "google")):
        result = _finalize(messages, [], None)
        for message in result:
            if isinstance(message.get("content"),list):
                for part in message["content"]:
                    part.pop("_final_sha256", None)
        return result, {"version":VERSION,"test_double":True}
    profile, tokenizer = runtime_profile(chat)
    context = getattr(underlying, "num_ctx", None)
    options = (getattr(chat,"kwargs",{}) or {}).get("options")
    if options is not None:
        if hasattr(options,"model_dump"):
            options = options.model_dump(exclude_none=True)
        context = options.get("num_ctx", context)
    if context != 8192:
        raise BudgetError("budget_profile_not_ready", "reviewed context must remain 8192")
    images = []
    for message in messages:
        content = message.get("content", "")
        for part in content if isinstance(content, list) else []:
            if part.get("type") == "image_url":
                meta = part.get("_budget_image")
                if meta:
                    with Image.open(meta.get("budget_source_path") or meta["input_path"]) as im:
                        image = im.convert("RGB")
                else:
                    uri = part["image_url"]["url"]
                    if not uri.startswith("data:image/"):
                        raise BudgetError("budget_profile_not_ready", "image bytes are unavailable for accounting")
                    image = Image.open(BytesIO(base64.b64decode(uri.split(",",1)[1]))).convert("RGB")
                images.append((part, image, meta))
    sizes = [tuple(meta["input_size"]) if meta else im.size for _, im, meta in images]
    candidates = []
    chosen = None
    for edge in (None, *EDGES):
        if edge and not any(max(s)>edge for s in sizes):
            continue
        candidate = []
        for size in sizes:
            ratio = min(1, edge/max(size)) if edge else 1
            candidate.append((max(1,round(size[0]*ratio)), max(1,round(size[1]*ratio))))
        budget = count(messages, schema, candidate, profile, tokenizer, output_limit, context)
        candidates.append({"max_edge":edge, **budget})
        if budget["fits"]:
            chosen = candidate
            break
    if chosen is None:
        details = {"task":task, "candidates":candidates}
        invocation_trace.add({"kind":"context_budget_rejected", **details})
        raise BudgetError("context_budget_exhausted", "輸入預算不足；已保留完整原文，未呼叫模型", details)
    budget.update(candidates=candidates, original_input_sizes=[list(s) for s in sizes], downscaled=chosen!=sizes)
    result = _finalize(messages, list(zip(images, chosen)), profile)
    final_budget = count(result, schema, chosen, profile, tokenizer, output_limit, context)
    if not final_budget["fits"]:
        raise BudgetError("context_budget_exhausted", "final transformed instructions exceed input budget", final_budget)
    budget.update(final_budget)
    budget["image_sha256"] = [part["_final_sha256"] for m in result for part in m.get("content",[]) if isinstance(part,dict) and "_final_sha256" in part]
    budget["image_artifacts"] = [{k:meta.get(k) for k in ("input_path","input_size","visual_size","source_size","sent_sha256","preprocessing")}
                                  for _,_,meta in images if meta]
    for m in result:
        if isinstance(m.get("content"),list):
            for p in m["content"]:
                p.pop("_final_sha256", None)
    budget["input_sha256"] = _sha(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return result, budget


def _finalize(messages, selected, profile):
    """Encode just the selected size. Coordinate metadata is updated in place."""
    result = deepcopy(messages)
    selections = iter(selected)
    for mi, message in enumerate(messages):
        if not isinstance(message.get("content"),list):
            continue
        for pi, part in enumerate(message["content"]):
            if part.get("type") != "image_url":
                continue
            if selected:
                (_, image, meta), size = next(selections)
            elif part.get("_budget_image"):
                meta = part["_budget_image"]
                image = Image.open(meta.get("budget_source_path") or meta["input_path"]).convert("RGB")
                size = tuple(meta["input_size"])
            else:
                continue
            if image.size != size:
                image = image.resize(size,Image.Resampling.LANCZOS)
            cached_path = Path(meta["input_path"]) if meta else None
            cached = cached_path.read_bytes() if cached_path and cached_path.is_file() and meta.get("sent_sha256") and tuple(meta["input_size"]) == size else None
            if cached is not None and _sha(cached) == meta["sent_sha256"]:
                payload = cached
            else:
                stream = BytesIO()
                image.save(stream, format="PNG")
                payload = stream.getvalue()
            if meta:
                old_instruction = model_coordinates.instructions(meta)
                base = Path(meta.setdefault("budget_artifact_base",meta["input_path"]))
                path = base.with_name(base.stem + "_" + _sha(payload)[:16] + ".png")
                if not path.is_file() or _sha(path.read_bytes()) != _sha(payload):
                    temporary = path.with_suffix(".tmp")
                    temporary.write_bytes(payload)
                    temporary.replace(path)
                meta.update(input_path=str(path),input_size=list(size), sent_sha256=_sha(payload))
                source_size = meta["source_size"]
                meta["preprocessing"]["source_to_input"] = [[size[0]/source_size[0],0,0],[0,size[1]/source_size[1],0]]
                meta["preprocessing"].setdefault("requested_max_edge",meta["preprocessing"].get("max_edge"))
                meta["preprocessing"]["max_edge"] = max(size)
                if profile:
                    meta.update(**model_coordinates.visual_frame(size, profile["processor"]))
                meta["budget_input_size"] = list(size)
                invocation_trace.add({"kind":"model_input_final", "context":meta})
                new_instruction = model_coordinates.instructions(meta)
                if old_instruction != new_instruction:
                    for item in result:
                        if isinstance(item.get("content"), str):
                            item["content"] = item["content"].replace(old_instruction,new_instruction)
                        elif isinstance(item.get("content"),list):
                            for entry in item["content"]:
                                if entry.get("type") == "text":
                                    entry["text"] = entry["text"].replace(old_instruction,new_instruction)
            result[mi]["content"][pi] = {"type":"image_url", "image_url":{"url":"data:image/png;base64,"+base64.b64encode(payload).decode()}, "_final_sha256":_sha(payload)}
    return result
