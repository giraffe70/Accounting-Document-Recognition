"""Parser and local-image security tests for the user manual PDF builder."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_user_manual import parse_manual, safe_image_path  # noqa: E402


class ManualBuilderTests(unittest.TestCase):
    def test_parser_extracts_main_content_and_ignores_page_shell_and_script(self):
        html = """<!doctype html><html><head><title>ignored</title></head><body>
        <aside><h2>sidebar ignored</h2></aside>
        <main id='main'><h1>手冊</h1><section><h2>章節</h2><p>正文<strong>重點</strong><br>續行</p>
        <script>alert('not content')</script><dialog>影像檢視工具</dialog><button>下載 PDF</button>
        <figure><a href='javascript:alert(1)'><img src='img/中文.png'></a><figcaption>圖說</figcaption></figure>
        <table><thead><tr><th>欄</th></tr></thead><tbody><tr><td>值</td></tr></tbody></table></section></main>
        <footer>outside</footer></body></html>"""
        main = parse_manual(html)
        text = "".join(part for node in [main] for part in self._texts(node))
        self.assertIn("手冊", text)
        self.assertIn("正文", text)
        self.assertNotIn("sidebar ignored", text)
        self.assertNotIn("outside", text)
        self.assertNotIn("alert('not content')", text)
        self.assertNotIn("影像檢視工具", text)
        self.assertNotIn("下載 PDF", text)
        images = [n for n in self._nodes(main) if n.tag == "img"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].attrs["src"], "img/中文.png")

    def test_missing_main_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "main id"):
            parse_manual("<html><body><main><p>missing id</p></main></body></html>")

    def test_safe_image_resolution_and_missing_file_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            manual = Path(temporary)
            image_dir = manual / "img"
            image_dir.mkdir()
            image = image_dir / "圖 1.png"
            image.write_bytes(b"test placeholder")
            self.assertEqual(safe_image_path("img/圖%201.png", image_dir), image.resolve())
            with self.assertRaises(FileNotFoundError):
                safe_image_path("img/missing.png", image_dir)

    def test_image_path_rejects_traversal_and_remote_sources(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            image_dir = Path(temporary) / "img"
            image_dir.mkdir()
            for src in ("../secret.png", "img/../secret.png", "img/%2e%2e/secret.png", "https://example.test/a.png", "//example.test/a.png", "/img/a.png"):
                with self.subTest(src=src), self.assertRaises(ValueError):
                    safe_image_path(src, image_dir)

    @staticmethod
    def _nodes(node):
        yield node
        for child in node.children:
            if not isinstance(child, str):
                yield from ManualBuilderTests._nodes(child)

    @staticmethod
    def _texts(node):
        for child in node.children:
            if isinstance(child, str):
                yield child
            else:
                yield from ManualBuilderTests._texts(child)


if __name__ == "__main__":
    unittest.main()
