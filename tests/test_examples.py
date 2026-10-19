import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest
import unittest.mock

from botroll import health as box
from botroll import serve
from botroll.board import page

ROOT = pathlib.Path(__file__).parent.parent
EXAMPLES = ROOT / "examples"


def screenshots():
    spec = importlib.util.spec_from_file_location("screenshots", ROOT / "scripts" / "screenshots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # Playwright is only imported when the script runs
    return mod


class ScreenshotFixtures(unittest.TestCase):
    """The invented data behind the screenshots stays valid as the formats change."""

    def setUp(self):
        self.shots = screenshots()
        self.at = self.shots.epoch(self.shots.NOW)

    def test_box_health_fixture_makes_valid_samples(self):
        spec = json.loads((EXAMPLES / "box-health.json").read_text(encoding="utf-8"))
        samples = self.shots.health_samples(spec, self.at)
        self.assertEqual(len(samples), 49)
        cleaned = [box.clean(s) for s in samples]  # raises on a bad sample
        self.assertEqual(cleaned[-1]["ts"], self.at)
        self.assertTrue(all(self.at - 24 * 3600 <= s["ts"] <= self.at for s in cleaned))
        for g in cleaned[-1]["browsers"][:3]:
            self.assertTrue(g["closable"] and g["main_pid"] and g["started_at"] < self.at)

    def test_box_health_panel_renders_from_the_fixture(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for f in (EXAMPLES / "tasks").glob("*.md"):
            shutil.copy(f, d)
        spec = json.loads((EXAMPLES / "box-health.json").read_text(encoding="utf-8"))
        pathlib.Path(d, box.HEALTH).write_text(json.dumps(self.shots.health_samples(spec, self.at)))
        with unittest.mock.patch.dict("os.environ", {"BOTROLL_NOW": self.shots.NOW, "BOTROLL_TZ": self.shots.TZ}):
            html = page(serve.served_snapshot(d), live=True, csrf="x" * 32, health_control=True)
        for want in ("demo-box · sampled just now", "STALE-HELPER", "Close browser", "busiest: Vera", "PID 41210"):
            self.assertIn(want, html)
        self.assertNotIn("Is the collector running?", html)

    def test_watch_findings_are_what_push_accepts(self):
        findings = json.loads((EXAMPLES / "watch-findings.json").read_text(encoding="utf-8"))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.assertEqual(serve.store_findings(d, findings), 3)
        self.assertEqual({f["kind"] for f in findings}, set(serve.PROCESS_KINDS))

    def test_only_fake_secrets(self):
        self.assertTrue(self.shots.FAKE == "FAKE_TOKEN_DO_NOT_USE" and self.shots.PASSWORD.startswith("FAKE_"))
        text = (ROOT / "scripts" / "screenshots.py").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "secret" in line.lower() and "=" in line and '"' in line and "FAKE" not in line:
                self.assertNotRegex(line, r'["\'][A-Za-z0-9_\-]{24,}["\']', line)


if __name__ == "__main__":
    unittest.main()
