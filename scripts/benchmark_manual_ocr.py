"""Explicit frozen-page manual/automatic pilot. Never imported by offline tests.

Cold means a new Python process per page (OCR initialization included); warm
means one excluded full-page warmup then new page caches in a resident process.
Ollama server load state is recorded, not forcibly unloaded on a shared host.
Direction/layout references lacking independent review cannot pass acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_inputs(manifest_path, directions_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    directions = json.loads(Path(directions_path).read_text(encoding="utf-8"))
    if sha(manifest["gold_path"]) != manifest["gold_sha256"]:
        raise ValueError("Frozen gold changed")
    expected = {p["id"] for p in manifest["pages"]}
    if len(expected) != len(manifest["pages"]) or set(directions["pages"]) != expected:
        raise ValueError("Direction review must cover exactly the manifest pages")
    for page in manifest["pages"]:
        case = directions["pages"][page["id"]]
        if sha(page["path"]) != page["sha256"]:
            raise ValueError("Frozen source image changed")
        if type(case.get("rotation_cw")) is not int or case["rotation_cw"] not in (0,90,180,270):
            raise ValueError("Every page needs an explicit clockwise direction")
        if case.get("document_mode") not in ("single","auto_split"):
            raise ValueError("Pilot needs an explicit single/auto_split decision")
    return manifest,directions


def candidate(payload, case, folder):
    from PIL import Image
    from app.core import pipeline, preparation, text_evidence, page_layout, image_evidence, doc_router
    from app.core import invocation_trace, model_invocation, ocr_cache, recognition_timing
    folder.mkdir(parents=True,exist_ok=True)
    working = folder / "working.png"
    with Image.open(payload["page_path"]) as image:
        image.convert("RGB").rotate(-case["rotation_cw"],expand=True).save(working)
    state = preparation.fresh()
    started = time.perf_counter()
    with invocation_trace.capture(folder / "trace.json") as trace, ocr_cache.scope(folder / "ocr_cache"):
        invocation_trace.add({"kind":"snapshot","versions":model_invocation.source_versions(),"parameters":payload})
        invocation_trace.add({"kind":"manual_direction_reference","reviewed":case.get("reviewed") is True,
                              "reference":case.get("reference"),"rotation_cw":case["rotation_cw"]})
        evidence = text_evidence.read(working,folder=folder,preparation=state,purpose="layout" if case["document_mode"]=="auto_split" else "extraction")
        if case["document_mode"] == "single":
            layout = {"type":case.get("type_key") or "other","rotate":0,"content_kind":"document","layout_kind":"single",
                      "auto_split":True,"issues":[],"regions":[{"box":[0,0,1000,1000],"rotate":0,"type":case.get("type_key") or ""}]}
        else:
            preview = page_layout.crop(working,{"box":[0,0,1000,1000]},folder / "preview.png",max_edge=1180)
            with Image.open(preview) as image:
                lines = image_evidence.map_lines(evidence["lines"],evidence["size"],image.size)
            layout = page_layout.detect(preview,native_path=working,ocr_lines=lines,ocr_text=evidence["text"],orientation_confirmed=True)
        documents = []
        if layout["auto_split"] and layout["content_kind"] == "document":
            for index,region in enumerate(layout["regions"]):
                path = page_layout.crop(working,region,folder / f"document_{index}.png")
                text_evidence.inherit(working,path,region,folder=folder)
                text = text_evidence.read(path,folder=folder,preparation=state)
                type_key = region.get("type")
                if not type_key:
                    type_key,_ = doc_router.classify(mode="ocr_text",ocr_text=text["text"])
                voucher = pipeline.extract_for_type(path,type_key,text_record=text,fast_mode=True,
                    confirmed_type=bool(case.get("type_key")))
                data = voucher.model_dump()
                documents.append({"id":uuid.uuid4().hex,"status":"succeeded","document_index":index,"voucher":data,
                                  "origin":page_layout.origin(working,region,payload["page_no"])})
        status = "succeeded" if documents else "skipped" if layout["auto_split"] else "partial"
        timings = {**recognition_timing.from_trace(trace),"complete_seconds":time.perf_counter()-started}
        return {"layout":layout,"documents":documents,"voucher":documents[0]["voucher"] if len(documents)==1 else {},
                "page_status":status,"processing_reason":"completed" if documents else "manual_split",
                "trace":trace,"timings":timings,"image_transforms":{"rotation":case["rotation_cw"]}}


def run_page(manifest,directions,page,label,folder):
    from app import validation
    from app.core import invocation_trace
    payload = {"id":uuid.uuid4().hex,"run_id":manifest[label+"_run_id"],"combo":manifest["combo"],
               "page_path":page["path"],"relative_path":page["source"],"page_no":page["page"],"max_edge":1180}
    started = time.perf_counter()
    try:
        if label == "candidate":
            response = {"ok":True,"data":candidate(payload,directions["pages"][page["id"]],folder)}
        else:
            def runner(part,timeout):
                phase_started = time.perf_counter()
                try:
                    return {"ok":True,"data":validation._recognize(part),"wall_seconds":time.perf_counter()-phase_started}
                except Exception as exc:
                    return {"ok":False,"error":f"{type(exc).__name__}: {exc}","wall_seconds":time.perf_counter()-phase_started}
            response = validation._run_page_child(payload,900,runner=runner)
    except Exception as exc:
        response = {"ok":False,"error":f"{type(exc).__name__}: {exc}"}
    result = {"page_id":page["id"],"source":page["source"],"page":page["page"],"image_sha256":page["sha256"],
              "wall_seconds":time.perf_counter()-started,"response":response}
    invocation_trace.atomic_json(folder / "row.json",result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",required=True)
    parser.add_argument("--directions",required=True)
    parser.add_argument("--output-dir",required=True)
    parser.add_argument("--condition",choices=("cold","warm","both"),default="both")
    parser.add_argument("--execute",action="store_true",help="Explicitly authorize real OCR/VLM calls")
    parser.add_argument("--worker",choices=("baseline","candidate"))
    parser.add_argument("--page-id")
    args = parser.parse_args()
    manifest,directions = load_inputs(args.manifest,args.directions)
    if not args.execute:
        parser.error("Real OCR/VLM requires --execute")
    output = Path(args.output_dir).resolve()
    if output.exists() and not args.worker:
        raise ValueError("Output must be new; saved results are never overwritten")
    output.mkdir(parents=True,exist_ok=True)
    from app import config
    from app.core import ocr_policy, settings_store, invocation_trace
    if args.worker:
        from app import validation
        config.UPLOAD_DIR = output / "uploads"
        validation.RUNS = output / "r"
        settings = {"ocr":"paddle/pp-ocrv5","structure":manifest["combo"]["structure_model"],"classify":manifest["combo"]["classify_model"]}
        pages = [p for p in manifest["pages"] if not args.page_id or p["id"]==args.page_id]
        with settings_store.isolated_settings(settings), ocr_policy.isolated_policy({"orientation_mode":"legacy","resolution":"fixed_2400"}):
            if args.condition == "warm":
                run_page(manifest,directions,pages[0],args.worker,output / "warmup")
            rows = []
            for page in pages:
                rows.append(run_page(manifest,directions,page,args.worker,output / page["id"]))
                invocation_trace.atomic_json(output / "rows.json",rows)
                print(f'{args.condition} {args.worker} {len(rows)}/{len(pages)} ok={rows[-1]["response"].get("ok")} seconds={rows[-1]["wall_seconds"]:.1f}',flush=True)
        return
    from app.core import model_invocation
    snapshot = model_invocation.source_versions()
    commit = subprocess.check_output(["git","-c",f"safe.directory={ROOT.as_posix()}","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    manifest = {**manifest,"baseline_run_id":uuid.uuid4().hex[:12],"candidate_run_id":uuid.uuid4().hex[:12],
                "baseline_commit":commit,"source_snapshot":snapshot,"worktree_snapshot_sha256":snapshot["code_version_sha256"]}
    invocation_trace.atomic_json(output / "manifest.json",manifest)
    args.manifest = str(output / "manifest.json")
    conditions = ("cold","warm") if args.condition=="both" else (args.condition,)
    for condition in conditions:
        for label in ("baseline","candidate"):
            phase = output / condition / label
            tasks = manifest["pages"] if condition == "cold" else [None]
            rows = []
            for number,page in enumerate(tasks,1):
                # Keep Paddle inputs, direction scratch files and disk locks
                # below Windows' legacy path limit; phase envelopes stay readable.
                worker_output = output / condition[0] / label[0] / str(number)
                worker_output.mkdir(parents=True,exist_ok=True)
                command = [sys.executable,"-B","-X","utf8",str(Path(__file__).resolve()),"--manifest",args.manifest,
                           "--directions",args.directions,"--output-dir",str(worker_output),"--condition",condition,"--worker",label,"--execute"]
                if page:
                    command += ["--page-id",page["id"]]
                with (worker_output / "worker.log").open("w",encoding="utf-8") as log:
                    subprocess.run(command,stdout=log,stderr=log,check=True,timeout=900 if page else 900*(len(manifest["pages"])+1))
                rows.extend(json.loads((worker_output / "rows.json").read_text(encoding="utf-8")))
                invocation_trace.atomic_json(phase / "envelope.json",{"run_id":manifest[label+"_run_id"],"combo":manifest["combo"],
                    "frozen_commit":manifest.get("baseline_commit"),"results":rows,"condition":condition,
                    "directions_sha256":sha(args.directions),"human_review_verified":all(v.get("reviewed") is True for v in directions["pages"].values())})
                print(f'{condition} {label}: {len(rows)}/{len(manifest["pages"])}',flush=True)
        from scripts.compare_ocr_pilot import compare
        compare(Path(args.manifest),output/condition/"baseline"/"envelope.json",output/condition/"candidate"/"envelope.json",output/condition/"comparison")
    if sha(manifest["gold_path"]) != manifest["gold_sha256"]:
        raise ValueError("Gold changed during pilot")


if __name__ == "__main__":
    main()
