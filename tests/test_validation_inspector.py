"""Offline interaction checks for the validation result inspector's production JS."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "js" / "validation.js"
APP_CSS = ROOT / "app" / "static" / "css" / "validation.css"
HARNESS = ROOT / "tests" / "fixtures" / "validation_inspector_synthetic.js"


def fixture() -> dict[str, object]:
    return {
        "docTypes": {
            "invoice": {
                "label": "統一發票",
                "fields": [
                    {"key": "voucher_id", "label": "發票號碼"},
                    {"key": "date", "label": "發票日期"},
                    {"key": "total_amount", "label": "含稅總額"},
                ],
                "item_fields": [
                    {"key": "description", "label": "品名"},
                    {"key": "amount", "label": "金額"},
                ],
                "entry_fields": [
                    {"key": "account", "label": "會計科目"},
                    {"key": "debit", "label": "借方"},
                    {"key": "credit", "label": "貸方"},
                ],
            }
        },
        "result": {
            "id": "synthetic-page-1",
            "relative_path": "synthetic/invoice.png",
            "page_no": 1,
            "status": "succeeded",
            "processing_reason": "completed",
            "combo_name": "mock",
            "mode": "ocr_vlm",
            "actual_path": "mock/offline",
            "artifacts": [
                {"kind": "original", "url": "/api/validation/artifacts/synthetic/page-original.png"},
            ],
            "trace": [{"kind": "fixture", "text": "長追蹤證據 " + "x" * 9000}],
            "documents": [
                {
                    "id": "synthetic-doc-1", "document_index": 0,
                    "status": "succeeded", "processing_reason": "completed",
                    "origin": {"region": [100, 200, 600, 800], "rotation": 0},
                    "artifacts": [
                        {"kind": "original", "url": "/api/validation/artifacts/synthetic/doc-1-crop.png"},
                        {"kind": "input", "url": "/api/validation/artifacts/synthetic/doc-1-model-input.png"},
                    ],
                    "voucher": {
                        "doc_type_key": "invoice", "doc_type": "統一發票", "voucher_id": "V-001",
                        "fields": {"voucher_id": "V-001", "date": "2026-10-01", "total_amount": 105},
                        "items": [{"description": "咖啡", "amount": 105}],
                        "entries": [{"account": "辦公用品", "debit": 105, "credit": 0}],
                    },
                    "trace": [{"kind": "fixture", "text": "doc trace"}],
                },
                {
                    "id": "synthetic-doc-2", "document_index": 1,
                    "status": "partial", "processing_reason": "document_partial_failure",
                    "origin": {"region": [650, 100, 900, 500], "rotation": 0},
                    "artifacts": [
                        {"kind": "original", "url": "/api/validation/artifacts/synthetic/doc-2-crop.png"},
                        {"kind": "input", "url": "/api/validation/artifacts/synthetic/doc-2-model-input.png"},
                    ],
                    "voucher": {
                        "doc_type_key": "invoice", "doc_type": "統一發票", "voucher_id": "V-002",
                        "fields": {"voucher_id": "V-002", "date": "2026-10-02", "total_amount": 220},
                        "items": [], "entries": [],
                    },
                },
            ],
        },
        "run": {
            "id": "synthetic-run", "name": "offline fixtures", "status": "completed", "done": 2, "total": 5,
            "latest_summary": {
                "done": 2, "total": 5, "succeeded": 1,
                "manual_split_count": 1, "document_partial_failure_count": 1,
                "insufficient_page_count": 0, "failed": 0, "skipped": 0,
                "integrity_ok": True,
            },
            "latest_statistics": [{
                "combo_name": "mock", "actual_path": "mock/offline", "integrity_ok": True,
                "completed_rate": 0.5, "processed_samples": 2, "extraction_completed": 1,
                "scheduled_samples": 5, "selected_samples": 5,
                "document_count": 2, "insufficient_count": 0, "location_issue_count": 0,
                "needs_review_count": 0, "warning_page_count": 0, "warning_document_count": 0,
                "quality_unassessed_count": 0, "first_seconds": 1, "average_seconds": 1,
                "p50_seconds": 1, "p95_seconds": 1, "throughput_per_minute": 1,
            }],
            "settings": {"combinations": [{"id": "combo-1", "name": "mock"}]},
        },
    }


class ValidationInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is not available")
        cls.source = APP_JS.read_text(encoding="utf-8")
        cls.css = APP_CSS.read_text(encoding="utf-8")
        cls.fixture = fixture()

        completed = subprocess.run(
            [cls.node, str(HARNESS)],
            input=json.dumps({"source": cls.source, **cls.fixture}, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
        cls.rendered = json.loads(completed.stdout.decode("utf-8"))

    def test_reason_labels_and_contract_labels_are_rendered_in_chinese(self):
        rendered = "".join(self.rendered["reasonHtml"])
        for label in ("等待人工分割", "單據部分擷取失敗", "擷取完成但資料不足"):
            self.assertIn(label, rendered)

        html = self.rendered["initialHtml"]
        for label in ("發票號碼", "發票日期", "含稅總額", "品名", "金額", "會計科目", "借方", "貸方"):
            self.assertIn(f">{label}</th>", html)
        for value in ("V-001", "咖啡", "辦公用品"):
            self.assertIn(value, html)

    def test_original_page_crop_and_model_input_are_distinct(self):
        html = self.rendered["initialHtml"]
        self.assertIn('src="/api/validation/artifacts/synthetic/page-original.png"', html)
        self.assertIn('src="/api/validation/artifacts/synthetic/doc-1-crop.png"', html)
        self.assertIn('src="/api/validation/artifacts/synthetic/doc-1-model-input.png"', html)
        self.assertIn("單據影像（裁切圖／模型輸入圖）", html)

    def test_normalized_region_overlay_moves_with_zoom_and_selects_document(self):
        html = self.rendered["initialHtml"]
        self.assertIn('viewBox="0 0 1000 1000"', html)
        self.assertIn('data-document-index="0" x="100" y="200" width="500" height="600"', html)
        self.assertIn('id="pageRegionSvg"', html)
        self.assertIn('id="pageImageCanvas"', html)
        self.assertIn('id="pageRegionSvg"', html[html.index('id="pageImageCanvas"'):])
        self.assertIn("scale(1)", self.rendered["initialCanvasTransform"])
        self.assertIn("scale(1.25)", self.rendered["zoomedCanvasTransform"])
        self.assertIn('class="document-region active" data-document-index="1"', self.rendered["selectedHtml"])
        self.assertIn("V-002", self.rendered["selectedHtml"])

    def test_technical_json_is_collapsed_and_long_pre_keeps_wrapped_whitespace(self):
        html = self.rendered["initialHtml"]
        self.assertRegex(html, r'<details class="detail-box"><summary>頁面 OCR 與模型追蹤</summary><pre class="diagnostic-pre">')
        self.assertNotRegex(html, r'<details[^>]*\bopen(?:\s|>)')
        self.assertIn("長追蹤證據", html)
        self.assertRegex(self.css, r"\.detail-box pre\s*\{[^}]*max-height:320px[^}]*white-space:pre-wrap")

    def test_processed_scheduled_counts_and_completion_rate_are_explicit(self):
        html = self.rendered["statsHtml"]
        self.assertEqual(self.rendered["runTitle"].split("｜")[-1], "已處理／排程 2/5")
        self.assertIn("已處理 2", html)
        self.assertIn("擷取完成 1", html)
        self.assertIn("排程 5", html)
        self.assertIn("擷取完成率（已處理非略過頁） 50.0%", html)

    def test_production_timing_block_handles_current_legacy_and_incomplete_records(self):
        cases = self.rendered["timingHtml"]
        version2 = cases["version2"]
        self.assertIn("方向判斷 OCR</th><td>1.25 秒</td>", version2)
        self.assertIn("補讀 OCR</th><td>未記錄</td>", version2)

        legacy = cases["legacy"]
        self.assertIn("方向判斷 OCR</th><td>未記錄</td>", legacy)
        self.assertIn("排隊</th><td>1.25 秒</td>", legacy)
        self.assertIn("總耗時</th><td>4.50 秒</td>", legacy)

        incomplete = cases["incomplete"]
        self.assertIn("總耗時</th><td>3.50 秒</td>", incomplete)
        self.assertIn("處理曾中斷，細項僅包含已完成的呼叫，耗時可能未完整記錄", incomplete)


if __name__ == "__main__":
    unittest.main()
