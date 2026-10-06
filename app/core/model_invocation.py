"""Shared local parsing, transport retries and evidence-preserving contract retries."""
from __future__ import annotations
import json
import logging
from copy import deepcopy
from app.core import invocation_trace
logger = logging.getLogger("pdf_scan.model_invocation")
def _invoke_text(chat, content, tries: int = 3) -> str:
    """呼叫 VLM 並取回文字；空回應或例外時重試。

    地端 Ollama 偶發性失敗（實測見過 CUDA error 500、以及回傳空字串），
    單次失敗就讓整張憑證辨識中斷太脆弱——批次提取時等於整批少一張。
    """
    import time
    from app.core import invocation_trace

    last = None
    for i in range(tries):
        try:
            with invocation_trace.timed_call(content, i + 1) as traced:
                resp = chat.invoke([{"role": "user", "content": content}])
                text = resp.content if hasattr(resp, "content") else str(resp)
                if text is None:
                    text = ""
                elif isinstance(text, list):
                    text = "\n".join(part.get("text", "") for part in text if isinstance(part, dict) and part.get("type") == "text")
                traced["response"] = str(text)
            if str(text).strip():
                return text
            last = "空回應"
        except Exception as exc:  # noqa: BLE001
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
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            pass
        # 後面還接了東西（qwen3 等會在 JSON 之後補說明或再吐一個物件，
        # 造成 "Extra data"）→ 只取第一個完整物件，忽略其餘。
        try:
            obj, _ = json.JSONDecoder().raw_decode(c.lstrip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    logger.warning("VLM JSON 解析失敗，原始輸出前 600 字：%s", frag[:600])
    raise json.JSONDecodeError("VLM 回傳非合法 JSON", frag, 0)


def invoke_structured(chat, content, validator, task="extract", invoke=None) -> dict:
    """At most two semantic rounds, each with at most three transport attempts."""
    caller = invoke or _invoke_text
    errors = ""
    for round_no in (1, 2):
        request = deepcopy(content)
        if round_no == 2:
            instruction = "上次輸出未通過契約。依原任務及全部原證據重新輸出；只修正以下錯誤：\n" + errors
            if isinstance(request, list):
                request.append({"type": "text", "text": instruction})
            else:
                request += "\n" + instruction
        invocation_trace.add({"kind": "structured_round", "task": task, "round": round_no, "errors": errors})
        raw = caller(chat, request)
        try:
            data = _parse_json(str(raw))
            if not isinstance(data, dict):
                raise ValueError("root must be an object")
            return validator(data)
        except (ValueError, TypeError) as exc:
            # Pydantic diagnostics include input values; record field locations/types only.
            if hasattr(exc, "errors"):
                errors = "; ".join(".".join(map(str,e["loc"])) + ":" + e["type"] for e in exc.errors())
            else:
                errors = str(exc)[:800]
            invocation_trace.add({"kind": "contract_error", "task": task, "round": round_no, "errors": errors})
    raise ValueError(f"{task} output invalid after 2 rounds: {errors}")


def source_versions() -> dict:
    """Versioned source/runtime digests, without exposing company/policy contents."""
    import hashlib
    from pathlib import Path
    from app import config
    root = Path(__file__).resolve().parents[1]
    names = {p.relative_to(root).as_posix() for p in (root / "core").glob("*.py")}
    names.update({"models/schemas.py", "services/accounting_policy.py", "services/classify.py",
                  "services/importdecl.py", "services/accounts.py", "services/taxid.py", "validation.py"})
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(names)}
    runtime = {name: hashlib.sha256((config.DATA_DIR / name).read_bytes()).hexdigest()
               if (config.DATA_DIR / name).exists() else None
               for name in ("accounting_policies.json", "accounts.json", "companies.json", "entry_examples.json")}
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    prompt_sources = {key: sources[key] for key in ("core/prompts.py", "core/doc_types.py", "core/doc_router.py")}
    return {"hash_version": 2, "source_sha256": sources, "runtime_sha256": runtime,
            "prompt_version_sha256": digest(prompt_sources), "code_version_sha256": digest(sources),
            "policy_version_sha256": digest({"source": sources["services/accounting_policy.py"], "runtime": runtime}),
            "execution_version_sha256": digest({"sources": sources, "runtime": runtime})}


def same_execution_version(saved: dict) -> bool:
    return saved.get("hash_version") == 2 and saved.get("execution_version_sha256") == source_versions()["execution_version_sha256"]
