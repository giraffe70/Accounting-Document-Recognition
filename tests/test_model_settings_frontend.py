"""Behavior tests for the model-settings modal's local-only choices."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "js" / "app.js"
NODE = Path(r"C:\Program Files\nodejs\node.exe")

SETTINGS_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = input.source;
const start = source.indexOf('  $("settingsBtn").addEventListener("click", openSettings)');
const end = source.indexOf('  // ── 日記帳 ──', start);
if (start < 0 || end < 0) throw new Error('could not locate settings modal functions');

const events = [], requests = [], nodes = {};
function makeNode(id) {
  const node = {
    id, _html: '', value: '', handlers: {},
    classList: { _set: new Set(['hidden']), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); }, contains(c) { return this._set.has(c); } },
    addEventListener(event, callback) { this.handlers[event] = callback; },
    get innerHTML() { return this._html; },
    set innerHTML(html) {
      this._html = html;
      if (id.startsWith('sel-')) {
        this.options = [...html.matchAll(/<option value="([^"]*)"([^>]*)>(.*?)<\/option>/g)].map((match) => ({
          value: match[1], disabled: /\bdisabled\b/.test(match[2]), selected: /\bselected\b/.test(match[2]), text: match[3],
        }));
        const selected = this.options.find((option) => option.selected) || this.options[0];
        this.value = selected ? selected.value : '';
      }
    },
  };
  return node;
}
function $(id) { return nodes[id] || (nodes[id] = makeNode(id)); }
const modal = $('modal');
function esc(value) { return value == null ? '' : String(value); }
function json() { return { 'Content-Type': 'application/json' }; }
async function responseError(resp, fallback) {
  const body = await resp.json();
  return (body && body.detail) || `${fallback}（HTTP ${resp.status}）`;
}
function alert(message) { events.push({ type: 'alert', message }); }
async function fetch(url, options = {}) {
  requests.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
  if ((options.method || 'GET') === 'GET' && url.startsWith('/api/models')) return { ok: true, json: async () => input.models };
  if ((options.method || 'GET') === 'GET' && url === '/api/config') return { ok: true, json: async () => input.config };
  if (input.networkFailure) throw new Error('offline');
  return { ok: input.postOk !== false, status: input.postStatus || 500, json: async () => input.postBody || { detail: 'server rejected config' } };
}

const context = vm.createContext({ console, input, events, requests, nodes, $, modal, esc, json, responseError, alert, fetch });
vm.runInContext(source.slice(start, end) + '\nglobalThis.api = { openSettings, fillSelect, saveSettings };', context);
(async () => {
  await context.api.openSettings();
  const action = input.action;
  if (action === 'save' || action === 'cancel') {
    if (input.selected) for (const [id, value] of Object.entries(input.selected)) $(id).value = value;
    if (action === 'save') await $('saveSettings').handlers.click();
    else $('closeModal').handlers.click();
  }
  const selects = Object.fromEntries(['sel-ocr', 'sel-structure', 'sel-classify'].map((id) => [id, {
    value: $(id).value, html: $(id).innerHTML, options: $(id).options || [],
  }]));
  process.stdout.write(JSON.stringify({ requests, events, selects, modalOpen: !modal.classList.contains('hidden') }));
})().catch((error) => { console.error(error.stack || error); process.exit(1); });
"""


class ModelSettingsFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")
        if not NODE.is_file():
            raise unittest.SkipTest(f"Node.js not found at {NODE}")

    def run_settings(self, **scenario):
        payload = {"source": self.source, **scenario}
        completed = subprocess.run(
            [str(NODE), "-e", SETTINGS_HARNESS],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        return json.loads(completed.stdout.decode("utf-8"))

    @staticmethod
    def mixed_models():
        return {
            "ocr": ["paddle/pp-ocrv5", "ollama/not-ocr", "google/cloud-ocr"],
            "ocr_status": {"available": True, "detail": "ready"},
            "structure": ["ollama/local-vlm", "h200/remote-vlm", "google/cloud-vlm"],
            "classify": ["ollama/local-llm", "openai/cloud-llm"],
        }

    def test_open_requests_local_models_and_shows_only_local_options(self):
        result = self.run_settings(
            action="inspect",
            models=self.mixed_models(),
            config={"ocr": "paddle/pp-ocrv5", "structure": "ollama/local-vlm", "classify": "ollama/local-llm"},
        )

        self.assertEqual([request["url"] for request in result["requests"]], ["/api/models?local_only=true", "/api/config"])
        self.assertEqual([select["value"] for select in result["selects"].values()], ["paddle/pp-ocrv5", "ollama/local-vlm", "ollama/local-llm"])
        self.assertNotIn("google/", "".join(select["html"] for select in result["selects"].values()))
        self.assertNotIn("h200/", "".join(select["html"] for select in result["selects"].values()))
        self.assertTrue(result["modalOpen"])
        self.assertFalse(any(request["method"] == "POST" for request in result["requests"]))

    def test_old_cloud_config_is_blank_and_cannot_be_silently_saved(self):
        result = self.run_settings(
            action="save",
            models=self.mixed_models(),
            config={"ocr": "google/cloud-ocr", "structure": "h200/remote-vlm", "classify": "openai/cloud-llm"},
        )

        self.assertEqual([result["selects"][name]["value"] for name in ("sel-ocr", "sel-structure", "sel-classify")], ["", "", ""])
        for select in result["selects"].values():
            self.assertIn("請選擇地端模型", select["html"])
            self.assertTrue(select["options"][0]["disabled"])
            self.assertTrue(select["options"][0]["selected"])
        self.assertFalse(any(request["method"] == "POST" for request in result["requests"]))
        self.assertTrue(result["modalOpen"])
        self.assertEqual([event["message"] for event in result["events"]], ["請先選擇各階段的地端模型"])

    def test_empty_local_lists_show_empty_placeholder_and_block_save(self):
        models = {"ocr": ["google/cloud-ocr"], "structure": ["h200/remote-vlm"], "classify": ["openai/cloud-llm"]}
        result = self.run_settings(action="save", models=models, config={})

        for select in result["selects"].values():
            self.assertIn("無可用的地端模型", select["html"])
            self.assertEqual(select["value"], "")
        self.assertFalse(any(request["method"] == "POST" for request in result["requests"]))
        self.assertTrue(result["modalOpen"])

    def test_valid_local_selections_save_and_success_closes_modal(self):
        selected = {"sel-ocr": "paddle/pp-ocrv5", "sel-structure": "ollama/local-vlm", "sel-classify": "ollama/local-llm"}
        result = self.run_settings(
            action="save", models=self.mixed_models(), config={"ocr": selected["sel-ocr"], "structure": selected["sel-structure"], "classify": selected["sel-classify"]},
        )

        post = result["requests"][-1]
        self.assertEqual(post["url"], "/api/config")
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["body"], {"ocr": selected["sel-ocr"], "structure": selected["sel-structure"], "classify": selected["sel-classify"]})
        self.assertFalse(result["modalOpen"])

    def test_cancel_and_failed_saves_leave_expected_modal_state(self):
        models = self.mixed_models()
        config = {"ocr": "paddle/pp-ocrv5", "structure": "ollama/local-vlm", "classify": "ollama/local-llm"}
        cancelled = self.run_settings(action="cancel", models=models, config=config)
        self.assertFalse(cancelled["modalOpen"])
        self.assertFalse(any(request["method"] == "POST" for request in cancelled["requests"]))

        for failure in ({"postOk": False, "postStatus": 503}, {"networkFailure": True}):
            with self.subTest(failure=failure):
                failed = self.run_settings(action="save", models=models, config=config, **failure)
                self.assertTrue(failed["modalOpen"])
                self.assertTrue(any(event["type"] == "alert" and "模型設定儲存失敗" in event["message"] for event in failed["events"]))
                self.assertEqual([request["method"] for request in failed["requests"]].count("POST"), 1)


if __name__ == "__main__":
    unittest.main()
