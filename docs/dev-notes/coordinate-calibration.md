# Model coordinate calibration

`scripts/calibrate_model_coordinates.py` measures whether the model returns
boxes in pixels or in a 0–1000 normalized frame. It sends six generated images
with labeled colored rectangles twice each, with a hard limit of 12 model
requests and no retries.

The processor must be present in `app.core.model_coordinates.CHECKED_PROCESSORS`
for the exact Ollama server version. For Ollama 0.35.0, the checked Qwen
processor uses factor 28, minimum 802816 pixels, maximum 3211264 pixels, and
`ceil_center` padding. Its 28-pixel factor comes from Qwen patch size 14 times
spatial merge size 2; its pixel limits come from the 1024–4096 image-token
limits. The tool also checks `/api/show` to confirm the model is the matching
`qwen25vl` architecture. The allowlist records source URLs and SHA-256 hashes
for the Ollama server pin and relevant llama.cpp processor files. The tool
calls the core validator, downloads every listed provenance URL, and checks
every byte hash before a model request. An unknown version, changed parameter,
wrong model architecture, missing source, or hash mismatch stops with zero
model calls.

Build the UTF-8 processor JSON from the exact entry in
`CHECKED_PROCESSORS["0.35.0"]`, preserving its complete `provenance` map and
primary `source_url`/`source_sha256`. Add the exact runtime identity from
`/api/version` and `/api/tags`:

```json
{
  "version": 1,
  "provider": "ollama",
  "backend_url": "http://localhost:11434",
  "server_version": "0.35.0",
  "model": "qwen2.5vl:7b",
  "model_digest": "<exact digest from /api/tags>",
  "processor": {
    "kind": "qwen_smart_resize",
    "server_version": "0.35.0",
    "factor": 28,
    "min_pixels": 802816,
    "max_pixels": 3211264,
    "padding_mode": "ceil_center",
    "source_url": "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/mtmd-image.cpp",
    "source_sha256": "7dc760e4f85e38e11d11268ae412c3b6c0a32389c6c584ca34dd7b4a679f950b",
    "provenance": {
      "https://raw.githubusercontent.com/ollama/ollama/v0.35.0/llm/llama_server.go": "10777cca3193c12b4cfcc90f5cfd88085c298f7a2c2c7333b0b95bb6e545d391",
      "https://raw.githubusercontent.com/ollama/ollama/v0.35.0/LLAMA_CPP_VERSION": "55b8f69cefcfc8fb7ddec8b5908e2e015d35f712c42b8b13fcf7f4417b3410b2",
      "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/clip.cpp": "f50b204fa1bd4138c53d041663bbaf1472bab3af3407b95a8023429eb14e51ce",
      "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/clip-model.h": "f4df467f21eb5ac4954010a3be30f26586ebe7fa77f6000e65a943215be011e2",
      "https://raw.githubusercontent.com/ggml-org/llama.cpp/b11081/tools/mtmd/mtmd-image.cpp": "7dc760e4f85e38e11d11268ae412c3b6c0a32389c6c584ca34dd7b4a679f950b"
    }
  }
}
```

The processor fields and hashes must exactly match the core allowlist. Each run
needs a new output directory. It contains the six synthetic PNGs,
`calibration_report.json`, one
`calls/call-NN.json` per attempted model request, and a profile only if every
observation passes. Call evidence includes the complete raw backend response,
token counts and finish reason, without storing the submitted image bytes.
The cumulative report is flushed before and after each call, so failures retain
the completed evidence. `--dry-run` checks runtime and processor provenance but
does not call the model or register a profile.

```powershell
conda run -n pdfscan312 python -B -X utf8 scripts/calibrate_model_coordinates.py `
  --processor-spec artifacts/coordinate-calibration/processor.json `
  --model qwen2.5vl:7b --base-url http://localhost:11434 `
  --output-dir artifacts/coordinate-calibration/run-2026-10-11

conda run -n pdfscan312 python -B -X utf8 scripts/calibrate_model_coordinates.py `
  --processor-spec artifacts/coordinate-calibration/processor.json `
  --model qwen2.5vl:7b --base-url http://localhost:11434 `
  --output-dir artifacts/coordinate-calibration/dry-run --dry-run
```

Scoring maps each predicted visual-frame box through the checked affine
transform, including center padding, into the original image's normalized
1000-coordinate frame. The expected rectangles are normalized directly from
their original synthetic image dimensions. No box-based scale fitting is used.
The report includes per-unit IoU values for all 12 observations. A profile is
registered only when exactly one unit has IoU at least 0.9 for every
observation and a final runtime metadata check still matches the initial
identity. Failed calibration leaves an existing profile registry untouched.
These synthetic checks do not establish recognition accuracy on real documents.

Enabled profiles also require an exact `input_preprocessing` rule match (version,
RGB/PNG encoding, LANCZOS, no upscaling, no adapter rotation, caller edge limit).
The runtime records the actual edge limit and image transform separately. A
missing or changed rule disables the profile, even if its server and model
identity still match. Update the rule version when changing this preprocessing.

## 2026-10-11 calibration result

The authorized 12 calls were completed against local Ollama **0.35.0**, model
`qwen2.5vl:7b`, digest
`5ced39dfa4bac325dc183dd1e4febaa1c46b3ea28bce48896c8e69c1e79611cc`.
All source hashes matched the version-pinned processor above. The visual-frame
dimensions came from that implementation, not from a model answer about image
size. Each target was called twice; both repeats returned the same coordinates.

| Synthetic target | Submitted size | Visual size | Pixel-frame IoU (each repeat) |
| --- | --- | --- | --- |
| 1 | 640 × 480 | 1036 × 784 | 0.9790 |
| 2 | 800 × 400 | 1288 × 644 | 0.9770 |
| 3 | 400 × 800 | 644 × 1288 | 0.9825 |
| 4 | 713 × 503 | 1092 × 756 | 0.9712 |
| 5 | 503 × 713 | 756 × 1092 | **0.8839** |
| 6 | 1024 × 768 | 1036 × 784 | 0.9757 |

The normalized-1000 interpretation also failed (minimum IoU 0). Absolute
pixel coordinates fit these observations better, but target 5 did not reach
the required 0.9. **Calibration failed; no enabled profile was registered.**
The threshold and processor were not fitted to the answers. No calls remain
within this task's 12-call allowance.

Full machine-readable evidence and raw replies are retained locally in
`app/data/coordinate-calibration-v3-20261011/calibration_report.json` and
`calls/`. These generated runtime artifacts are ignored by Git. The report
records submitted/visual sizes, padding, expected and returned boxes, per-call
IoU, token counts and finish reasons. It contains only synthetic targets.

Recognition can still use OCR positions and complete-page extraction. Multiple
documents can use validated OCR line groups; they require manual splitting if
independent complete-content evidence is insufficient. Model boxes and model
field locations remain disabled until a separately authorized calibration
passes all targets for the same runtime identity. Processor metadata by itself
does not enable them.

## Deploying the version 3 contracts

Stop the existing web service yourself and run `start_webui.bat` from the project
root using `pdfscan312`. Do not restart only a worker. After signing in, check
`/api/validation/runtime`: `ready=true`, `schema_ready=true`,
`restart_required=false`, identical `loaded_version`/`disk_version`, and
`worker_protocol=3`. Reload `/validation.html` with `Ctrl+F5` and create a new
batch; do not reuse an earlier batch to validate these contracts. See
[the service and export workflow](agent-workflow.md#驗證服務版本結果檢視與舊診斷).

The inspector defaults to the original page, with selectable region overlays,
zoom/pan controls, and Chinese field, item and journal-entry tables. Technical
JSON and trace are collapsed. Partial pages distinguish manual splitting,
page-role review and failed child extraction; insufficient data is a quality
state. Progress is processed pages over scheduled pages; queued or unprocessed
cancelled pages are not recognition failures. Legacy results retain their
original state and unassessed quality.

Offline verification on 2026-10-11 passed `scripts\verify.bat -Scope all`
(215 tests), `node --check app/static/js/validation.js` and `git diff --check`.
Tests used `pdfscan312`, UTF-8 and the writable `app/data/test-tmp-plan`
temporary directory; the verification entry blocked outbound model connections.
An isolated browser preview with synthetic records also checked desktop
selection/125% zoom/overlay alignment and the narrow-screen image/data tabs.
The private dataset, gold and historical results were not changed. The existing
web service was not restarted, and no private recognition batch was started.
