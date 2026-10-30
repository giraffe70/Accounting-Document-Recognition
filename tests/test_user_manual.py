"""Offline manual delivery checks; no production application startup or database."""
import tempfile
import unittest
from contextlib import ExitStack
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import user_manual

ROOT = Path(__file__).resolve().parents[1]


class ManualApiTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        (self.root / "img").mkdir()
        (self.root / "使用者手冊.html").write_text('<html><img src="img/範例.png"><img src="img/../private.png"></html>', encoding="utf-8")
        (self.root / "使用者手冊.pdf").write_bytes(b"%PDF-1.4\nsynthetic")
        (self.root / "img/範例.png").write_bytes(b"synthetic-png")
        (self.root / "img/未引用.png").write_bytes(b"not-published")
        (self.root / "private.png").write_bytes(b"private")
        stack.enter_context(patch.object(user_manual, "MANUAL_DIR", self.root))
        stack.enter_context(patch.object(user_manual.auth, "get_user_by_token", side_effect=lambda token:
            {"id": 1, "is_admin": token == "admin"} if token in {"ordinary", "admin"} else None))
        app = FastAPI()
        app.include_router(user_manual.router)
        self.client = stack.enter_context(TestClient(app))
        self.client.cookies.set("session", "ordinary")

    def test_all_published_files_require_login_and_allow_ordinary_users(self):
        for token, expected in (("", 401), ("invalid", 401), ("ordinary", 200), ("admin", 200)):
            self.client.cookies.set("session", token)
            for url in ("/api/manual/", "/api/manual/download", "/api/manual/img/範例.png"):
                with self.subTest(token=token, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, expected)
                    self.assertEqual(response.headers["cache-control"], "no-store")

    def test_html_is_inline_and_pdf_download_has_chinese_filename(self):
        html = self.client.get("/api/manual/")
        self.assertTrue(html.headers["content-type"].startswith("text/html"))
        self.assertNotIn("content-disposition", html.headers)
        pdf = self.client.get("/api/manual/download")
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertTrue(pdf.headers["content-disposition"].startswith("attachment;"))
        self.assertIn("憑證辨識系統使用者手冊.pdf", unquote(pdf.headers["content-disposition"]))
        self.assertTrue(pdf.content.startswith(b"%PDF-"))
        self.assertEqual(self.client.get("/api/manual").status_code, 200)

    def test_unreferenced_and_unsafe_paths_are_never_served(self):
        for url in ("/api/manual/img/未引用.png", "/api/manual/img/private.png",
                    "/api/manual/img/%2e%2e%2fprivate.png", "/api/manual/使用者手冊.docx",
                    "/api/manual/img/範例.png%00", "/api/manual/img/../private.png"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.client.request(method, "/api/manual/download").status_code, 405)

    def test_missing_deployed_files_return_clear_not_found(self):
        for relative, url in (("使用者手冊.pdf", "/api/manual/download"),
                              ("img/範例.png", "/api/manual/img/範例.png"),
                              ("使用者手冊.html", "/api/manual/")):
            (self.root / relative).unlink()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertIn("手冊", response.json()["detail"])


class ManualMarkupTests(unittest.TestCase):
    def test_manual_navigation_opens_separate_tab_on_both_user_pages(self):
        for relative in ("app/static/index.html", "app/static/confirmed.html"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("window.open('/api/manual/', '_blank', 'noopener')", source)
            self.assertIn("📘 使用者手冊", source)

    def test_every_manual_figure_and_anchor_resolves(self):
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.ids, self.links, self.images = set(), [], []
                self.figures = self.captions = 0

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("id"):
                    self.ids.add(attrs["id"])
                if tag == "a" and attrs.get("href", "").startswith("#"):
                    self.links.append(attrs["href"][1:])
                if tag == "figure":
                    self.figures += 1
                if tag == "figcaption":
                    self.captions += 1
                if tag == "img" and attrs.get("src"):
                    self.images.append(attrs)

        source = (ROOT / "docs/manual/使用者手冊.html").read_text(encoding="utf-8")
        parsed = Markup()
        parsed.feed(source)
        self.assertTrue(set(parsed.links).issubset(parsed.ids))
        self.assertEqual(parsed.figures, parsed.captions)
        self.assertGreaterEqual(parsed.figures, 12)
        for attrs in parsed.images:
            self.assertTrue(attrs.get("alt"))
            self.assertTrue((ROOT / "docs/manual" / attrs["src"]).is_file(), attrs["src"])
        self.assertIn('href="download"', source)
        self.assertIn("window.print()", source)
        self.assertIn("showModal()", source)
        self.assertNotIn("④ 確認的已確認範圍", source)


if __name__ == "__main__":
    unittest.main()
