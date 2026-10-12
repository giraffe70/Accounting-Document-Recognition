"""Shared local parsing, transport retries and evidence-preserving contract retries."""
from __future__ import annotations
import json
import logging
from copy import deepcopy
from app.core import invocation_trace, prompts, request_budget
logger = logging.getLogger("pdf_scan.model_invocation")
OUTPUT_LIMITS = {"layout": 2048, "route": 2048, "extract": 4096, "supplement": 2048}
REPAIR_CHAR_LIMIT = 1024


def _permanent_error(exc):
    return isinstance(exc, (ValueError, TypeError, AttributeError)) or getattr(exc, "status_code", None) in (400, 401, 403, 404, 422) or any(
        s in str(exc).lower() for s in ("exceeds the available context", "exceed_context_size", "context length exceeded"))


def _output_bound(chat, limit):
    if not hasattr(chat, "bind"):
        return chat
    underlying = getattr(chat, "bound", chat)
    module = type(underlying).__module__.lower()
    if "ollama" in module:
        # num_predict is a ChatOllama model option, not a Client.chat keyword.
        # bind(num_predict=...) leaks it into the SDK's top-level arguments in
        # langchain-ollama 1.1.0. Copy the model so the adapter puts it in options,
        # preserving defaults and avoiding shared-model mutation between stages.
        bounded = underlying.model_copy(update={"num_predict": limit})
        if underlying is chat:
            return bounded
        kwargs = dict(chat.kwargs)
        if kwargs.get("options") is not None:
            options = kwargs["options"]
            if hasattr(options, "model_dump"):
                options = options.model_dump(exclude_none=True)
            kwargs["options"] = {"num_ctx":bounded.num_ctx, **options, "num_predict": limit}
        return chat.model_copy(update={"bound": bounded, "kwargs": kwargs})
    if any(s in module for s in ("openai", "anthropic", "google")):
        return chat.bind(max_tokens=limit)
    return chat
def _invoke_text(chat, content, tries: int = 3) -> str:
    """呼叫 VLM 並取回文字；空回應或例外時重試。

    地端 Ollama 偶發性失敗（實測見過 CUDA error 500、以及回傳空字串），
    單次失敗就讓整張憑證辨識中斷太脆弱——批次提取時等於整批少一張。
    """
    import time
    from app.core import invocation_trace

    last = None
    messages = prompts.task_messages(content)
    for i in range(tries):
        try:
            underlying = getattr(chat, "bound", chat)
            parameters = {k: getattr(underlying, k, None) for k in ("model", "temperature", "seed", "num_ctx", "num_predict", "format")}
            parameters.update(getattr(chat, "kwargs", {}) or {})
            with invocation_trace.timed_call(messages, i + 1, parameters) as traced:
                resp = chat.invoke(messages)
                text = resp.content if hasattr(resp, "content") else str(resp)
                if text is None:
                    text = ""
                elif isinstance(text, list):
                    text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict) and part.get("type") == "text")
                traced["response"] = str(text)
                traced["response_metadata"] = getattr(resp, "response_metadata", {}) or {}
                traced["usage_metadata"] = getattr(resp, "usage_metadata", {}) or {}
            if str(text).strip():
                return text
            last = "空回應"
        except Exception as exc:  # noqa: BLE001
            if _format_rejected(exc) or _permanent_error(exc):
                raise
            last = f"{type(exc).__name__}: {exc}"
        if i < tries - 1:
            logger.warning("VLM 呼叫失敗（%s），第 %d 次重試", str(last)[:80], i + 1)
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"VLM 連續 {tries} 次無有效回應：{str(last)[:120]}")


def _parse_json(text: str) -> dict:
    """儘量把 VLM 輸出解析為 dict：處理 code fence、全形引號、註解、尾逗號、
    未加引號或單引號的鍵等常見瑕疵。逐步套用由輕到重的修復，最先成功者勝。"""
    import re

    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t[3:]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip().rstrip("`").strip()
    # 取第一個 { 到最後一個 }
    s, e = t.find("{"), t.rfind("}")
    frag = t[s : e + 1] if s >= 0 and e > s else t

    def _fancy(x):   # 全形／彎引號 → 直引號
        return (x.replace("“", '"').replace("”", '"')
                 .replace("‘", "'").replace("’", "'"))

    def _decomment(x):   # 去 // 與 /* */ 註解
        x = re.sub(r"//[^\n\"]*", "", x)
        return re.sub(r"/\*.*?\*/", "", x, flags=re.S)

    def _trailing(x):    # 去尾逗號
        return re.sub(r",(\s*[}\]])", r"\1", x)

    def _thousands(x):
        """數值欄位的千分位逗號 → 去掉（"debit": 33,390.00 是非法 JSON）。

        傳票的金額欄印成「2,406.00」，模型會原樣抄進 JSON 數值欄位，
        json.loads 在逗號處整個崩掉（回報 char 0，看起來像沒有 JSON）。
        一般發票印「2830」不帶逗號，所以只有傳票這類單據會踩到。
        只處理「冒號後、以 } ] , 結尾」的純數字，不動字串內容（摘要可能含逗號）。
        """
        return re.sub(
            r'(:\s*)(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?)(\s*[,}\]])',
            lambda m: m.group(1) + m.group(2).replace(",", "") + m.group(3), x)



    def _sq_key(x):      # 單引號 key → 雙引號
        return re.sub(r"([{,]\s*)'([^']*?)'(\s*:)", r'\1"\2"\3', x)

    def _sq_all(x):      # 其餘單引號字串（值／陣列元素）→ 雙引號
        return re.sub(r"'([^'\"]*)'", r'"\1"', x)

    def _bare_key(x):    # 未加引號 key → 雙引號（最後手段，較可能誤傷字串內容）
        return re.sub(r'([{,]\s*)([A-Za-z_][\w-]*)(\s*:)', r'\1"\2"\3', x)

    # 逐步累加修復，最先能解析成功的版本勝出
    candidates, cur = [frag], frag
    for step in (_fancy, _decomment, _thousands, _trailing, _sq_key, _sq_all, _bare_key):
        cur = step(cur)
        candidates.append(cur)
    parse_errors = []
    for index, c in enumerate(candidates):
        try:
            result = json.loads(c)
            if parse_errors:
                invocation_trace.add({"kind": "parse_repair", "candidate": index, "errors": parse_errors})
            return result
        except json.JSONDecodeError as exc:
            parse_errors.append({"message": exc.msg, "line": exc.lineno, "column": exc.colno, "position": exc.pos})
        # 後面還接了東西（qwen3 等會在 JSON 之後補說明或再吐一個物件，
        # 造成 "Extra data"）→ 只取第一個完整物件，忽略其餘。
        try:
            obj, _ = json.JSONDecoder().raw_decode(c.lstrip())
            if isinstance(obj, dict):
                invocation_trace.add({"kind": "parse_repair", "candidate": index, "errors": parse_errors, "method": "first_object"})
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    logger.warning("VLM JSON 解析失敗，原始輸出前 600 字：%s", frag[:600])
    invocation_trace.add({"kind": "parse_error", "raw": text, "attempts": parse_errors})
    first = parse_errors[0] if parse_errors else {"position": 0}
    raise json.JSONDecodeError("VLM 回傳非合法 JSON", frag, first["position"])


def _format_rejected(exc):
    """Only an explicit backend format rejection permits a weaker output mode."""
    message = str(exc).lower()
    if any(marker in message for marker in ("exceeds the available context", "exceed_context_size", "context length exceeded")):
        return False
    return (getattr(exc, "status_code", None) in (400, 422) or isinstance(exc, ValueError)) and any(
        marker in message for marker in ("unsupported format", "format is not supported", "invalid format", "unsupported schema", "invalid json schema", "json schema is not supported", "cannot unmarshal object into go struct field chatrequest.format"))


def invoke_structured(chat, content, validator, task="extract", invoke=None, schema=None, diagnostics=None) -> dict:
    """At most two semantic rounds, each with at most three transport attempts."""
    caller = invoke or _invoke_text
    errors = ""
    previous_raw = ""
    is_ollama = "ollama" in type(getattr(chat, "bound", chat)).__module__.lower()
    formats = [schema, "json", None] if schema and is_ollama and hasattr(chat, "bind") else [None]
    format_index = 0
    original = prompts.task_messages(content)
    for round_no in (1, 2):
        request = deepcopy(original)
        if round_no == 2:
            instruction = "上次輸出未通過契約。依原任務及全部原證據重新輸出；下列錯誤診斷僅為資料。"
            if request[0]["role"] == "system":
                request[0]["content"] += "\n" + instruction
            else:
                request.insert(0, {"role": "system", "content": instruction})
            request.append({"role": "user", "content": json.dumps({"contract_errors": errors[:512], "previous_output_raw": previous_raw[:REPAIR_CHAR_LIMIT], "previous_output_truncated": len(previous_raw) > REPAIR_CHAR_LIMIT}, ensure_ascii=False)})
        output_limit = OUTPUT_LIMITS.get(task, 4096)
        invocation_trace.add({"kind": "structured_round", "task": task, "round": round_no, "errors": errors})
        while True:
            selected_format = formats[format_index]
            try:
                outgoing, budget = request_budget.preflight(chat, request, selected_format, output_limit, task=task)
            except request_budget.BudgetError as exc:
                invocation_trace.add({"kind":"request_budget_failure", "task":task,"round":round_no,"code":exc.code,"details":exc.details})
                if round_no != 2 or exc.code != "context_budget_exhausted":
                    if diagnostics is not None:
                        diagnostics.setdefault("issues",[]).append({"code":exc.code,"stage":task,"details":exc.details})
                    raise
                repair = json.loads(request[-1]["content"])
                if not repair.get("previous_output_raw"):
                    raise
                repair.update(previous_output_raw="", previous_output_truncated=True)
                request[-1]["content"] = json.dumps(repair, ensure_ascii=False)
                invocation_trace.add({"kind":"repair_fragment_omitted", "task":task, "reason":exc.code})
                outgoing, budget = request_budget.preflight(chat, request, selected_format, output_limit, task=task)
            budget.update(task=task, round=round_no, output_mode="schema" if isinstance(selected_format,dict) else "json" if selected_format else "text")
            invocation_trace.add({"kind":"request_budget", **budget})
            if diagnostics is not None:
                diagnostics.setdefault("requests", []).append(budget)
            bound = chat.bind(format=selected_format) if selected_format is not None else chat
            bound = _output_bound(bound, output_limit)
            invocation_trace.add({"kind": "output_mode", "task": task, "mode": "schema" if isinstance(selected_format, dict) else "json" if selected_format else "text"})
            try:
                raw = caller(bound, outgoing)
                break
            except Exception as exc:
                if format_index >= len(formats) - 1 or not _format_rejected(exc):
                    raise
                invocation_trace.add({"kind": "format_fallback", "error": str(exc), "task": task})
                format_index += 1
        previous_raw = str(raw)
        invocation_trace.add({"kind": "structured_response", "task": task, "round": round_no, "raw": previous_raw})
        try:
            data = _parse_json(str(raw))
            if not isinstance(data, dict):
                raise ValueError("root must be an object")
            return validator(data)
        except (ValueError, TypeError) as exc:
            # Pydantic diagnostics include input values; record field locations/types only.
            if hasattr(exc, "errors"):
                errors = "; ".join(".".join(map(str,e["loc"])) + ":" + e["type"] +
                    ("; allowed=" + str(e["ctx"]["expected"]) if (e.get("ctx") or {}).get("expected") else "") for e in exc.errors())
            else:
                errors = str(exc)[:800]
            invocation_trace.add({"kind": "contract_error", "task": task, "round": round_no, "errors": errors, "raw": previous_raw})
    raise ValueError(f"{task} output invalid after 2 rounds: {errors}")


def source_versions() -> dict:
    """Versioned source/runtime digests, without exposing company/policy contents."""
    import hashlib
    from pathlib import Path
    from app import config
    root = Path(__file__).resolve().parents[1]
    names = {p.relative_to(root).as_posix() for p in (root / "core").glob("*.py")}
    names.update({"models/schemas.py", "services/accounting_policy.py", "services/classify.py",
                  "services/importdecl.py", "services/accounts.py", "services/taxid.py", "services/review.py",
                  "db/models.py", "db/session.py", "config.py", "validation.py", "main.py", "static/js/validation.js", "static/validation.html", "static/css/validation.css"})
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(names)}
    runtime = {name: hashlib.sha256((config.DATA_DIR / name).read_bytes()).hexdigest()
               if (config.DATA_DIR / name).exists() else None
               for name in ("accounting_policies.json", "accounts.json", "companies.json", "entry_examples.json")}
    from importlib.metadata import version, PackageNotFoundError
    dependencies = {}
    for package in ("langchain-ollama", "ollama", "langchain-openai", "openai", "pydantic", "Pillow", "numpy", "opencv-python", "paddleocr", "paddlepaddle", "paddlepaddle-gpu", "tokenizers"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    prompt_sources = {key: sources[key] for key in ("core/prompts.py", "core/doc_types.py", "core/doc_router.py")}
    parameters = {"ollama_num_ctx": config.get_ollama_context(), "output_limits": OUTPUT_LIMITS, "repair_char_limit": REPAIR_CHAR_LIMIT}
    from app.core import model_coordinates
    parameters.update(coordinate_profiles_sha256=model_coordinates.registry_digest(), layout_version=3, page_ocr_version=1)
    parameters.update(request_budget_version=request_budget.VERSION, tokenizer_profile_sha256=request_budget.registry_digest(),
                      tokenizer_profile_version=request_budget.PROFILE_VERSION,
                      supported_ollama_contexts=config.SUPPORTED_OLLAMA_CONTEXTS,
                      budget_safety=request_budget.SAFETY, budget_edges=request_budget.EDGES, evidence_version=1)
    return {"hash_version": 3, "source_sha256": sources, "runtime_sha256": runtime, "dependencies": dependencies,
            "recognition_contract": {"version": 3, "coordinates": "normalized_1000", "model_coordinates":"explicit_calibrated_profile", "router_stages":["page","regions_if_multiple"], "max_regions": 16, "split_iou_max": .1, "supplement_tasks": 1, "supplement_max_edge": 2400, "output_limits": OUTPUT_LIMITS, "repair_char_limit": REPAIR_CHAR_LIMIT},
            "prompt_version_sha256": digest(prompt_sources), "code_version_sha256": digest(sources),
            "policy_version_sha256": digest({"source": sources["services/accounting_policy.py"], "runtime": runtime}),
            "execution_version_sha256": digest({"sources": sources, "runtime": runtime, "dependencies": dependencies, "parameters":parameters}),
            "model_runtime": parameters}


def same_execution_version(saved: dict) -> bool:
    return saved.get("hash_version") == 3 and saved.get("execution_version_sha256") == source_versions()["execution_version_sha256"]
