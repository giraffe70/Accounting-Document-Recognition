"""Manually download and safely install PaddleX's document orientation model."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
from urllib.error import URLError
from urllib.request import Request, urlopen


MODEL_NAME = "PP-LCNet_x1_0_doc_ori"
SOURCE_URL = (
    "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/"
    "paddle3.0.0/PP-LCNet_x1_0_doc_ori_infer.tar"
)
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_COUNT = 1000


def _default_output() -> Path:
    return Path(__file__).resolve().parents[1] / "app" / "data" / "models" / "doc_ori"


def _download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "pdf-scan-recognition-model-setup/1"})
    total = 0
    with urlopen(request, timeout=60) as response, destination.open("xb") as stream:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError(f"模型封裝超過大小限制 ({MAX_ARCHIVE_BYTES} bytes)")
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())


def _member_path(stage: Path, name: str) -> Path:
    if not name or "\\" in name or "\x00" in name or ":" in name:
        raise ValueError(f"封裝含不安全路徑：{name!r}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError(f"封裝含不安全路徑：{name!r}")
    if any(part.endswith((".", " ")) for part in relative.parts):
        raise ValueError(f"封裝含 Windows 不安全路徑：{name!r}")
    target = stage.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(stage.resolve()):
        raise ValueError(f"封裝路徑超出暫存目錄：{name!r}")
    return target


def _extract_safely(archive_path: Path, stage: Path) -> Path:
    stage.mkdir()
    extracted_bytes = 0
    count = 0
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive:
            count += 1
            if count > MAX_MEMBER_COUNT:
                raise ValueError("模型封裝內檔案數超過安全上限")
            target = _member_path(stage, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"封裝包含不允許的連結或特殊檔案：{member.name!r}")
            extracted_bytes += member.size
            if member.size < 0 or extracted_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError("解壓內容超過安全大小上限")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"無法讀取封裝檔案：{member.name!r}")
            with source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)

    children = list(stage.iterdir())
    payload = children[0] if len(children) == 1 and children[0].is_dir() else stage
    if not (payload / "inference.pdiparams").is_file():
        raise ValueError("模型封裝缺少 inference.pdiparams")
    if not any((payload / name).is_file() for name in ("inference.yml", "inference.yaml", "inference.json")):
        raise ValueError("模型封裝缺少 inference.yml、inference.yaml 或 inference.json")
    return payload


def _manifest(payload: Path) -> dict:
    files = []
    for path in sorted(p for p in payload.rglob("*") if p.is_file()):
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        files.append({"path": path.relative_to(payload).as_posix(),
                      "size_bytes": size, "sha256": digest.hexdigest()})
    return {"model": MODEL_NAME, "source_url": SOURCE_URL,
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "files": files}


def install(output: Path) -> None:
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"輸出目錄已存在，拒絕覆寫：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".doc-ori-setup-", dir=output.parent) as temporary:
        work = Path(temporary)
        archive = work / "model.tar"
        _download(SOURCE_URL, archive)
        payload = _extract_safely(archive, work / "extracted")
        manifest = _manifest(payload)
        (payload / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.rename(payload, output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="明確手動下載官方 PP-LCNet_x1_0_doc_ori 推論模型並安全安裝。")
    parser.add_argument("--output-dir", type=Path, default=_default_output(),
                        help="新模型目錄（預設：app/data/models/doc_ori）；已存在時拒絕覆寫")
    args = parser.parse_args(argv)
    try:
        install(args.output_dir)
    except (OSError, ValueError, tarfile.TarError, URLError) as exc:
        print(f"模型安裝失敗：{exc}", file=sys.stderr)
        return 2
    print(f"已安裝 {MODEL_NAME} 至 {args.output_dir.expanduser().absolute()}")
    print("來源與解壓後檔案 SHA-256 已記錄於 manifest.json。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
