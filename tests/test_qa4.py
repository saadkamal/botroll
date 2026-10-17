"""QA round 4: labels a folder name can't fake (M2), and the per-install salt (S1, S2, T5)."""
import tmphome  # noqa: F401
import contextlib
import os
import pathlib
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from botroll import TIME, health, local
from botroll.board import _profile_labels, health_panel


def tmpdir(test):
    d = pathlib.Path(os.path.realpath(tempfile.mkdtemp()))
    test.addCleanup(shutil.rmtree, d, True)
    return d


class Labels(unittest.TestCase):
    def setUp(self):
        self.pid = health.profile_id("/home/a/Fork-3")

    @staticmethod
    def g(profile, pid, profile_id=None, bot="Zoe"):
        return {"profile": profile, "bot": bot, "procs": 1, "rss_mb": 1, "cpu": 0.0, "port": None,
                "main_pid": pid, "started_at": 1, "closable": True, "tabs": [], "profile_id": profile_id}

    def three(self):
        return [self.g("Fork-3", 5001, self.pid), self.g("Fork-3", 5002, self.pid),
                self.g(f"Fork-3 #{self.pid} pid 5001", 5003, "99999999")]

    def panel(self, browsers):
        data = tmpdir(self)
        health.append(str(data), {"v": 1, "ts": int(time.time()), "host": "box", "cores": 2, "load": [0.1, 0.1, 0.1],
                                  "mem": {"total_mb": 1000, "available_mb": 500}, "browsers": browsers,
                                  "helpers": [], "jobs": [], "alerts": []})
        return health_panel({"generated": local().strftime(TIME), "health": health.load(str(data))},
                            env={}, csrf="tok", control=True)

    def test_m2_labels_unique_even_when_a_folder_mimics_one(self):
        b = self.three()
        labels = _profile_labels(b, ["Zoe"] * 3)
        self.assertEqual(len(set(labels)), 3, labels)
        self.assertEqual(labels[0], f"Fork-3 #{self.pid} pid 5001")
        self.assertEqual(labels[2], f"Fork-3 #{self.pid} pid 5001 (2)")

    def test_m2_counter_skips_taken_labels(self):
        b = [self.g("A", 1), self.g("A", 1), self.g("A (2)", 2), self.g("A pid 1", 3)]
        labels = _profile_labels(b, ["Zoe"] * 4)
        self.assertEqual(len(set(labels)), 4, labels)

    def test_m2_three_rows_through_ingest_and_render(self):
        rows = self.panel(self.three()).split('<div class="h-br">')[1:]
        self.assertEqual(len(rows), 3)
        heads, confirms = [], []
        for row, real in zip(rows, (5001, 5002, 5003)):
            head = row.split('<span class="h-br-meta">')[0]
            heads.append(head.split('<span class="h-pid">')[0])
            self.assertIn(f'<span class="h-pid">PID {real}</span>', head)  # true PID in its own element
            btn = row.split('type="submit">')[1].split("</button>")[0]
            confirms.append(btn.split(' <span class="h-pid">')[0])
            self.assertTrue(btn.endswith(f'<span class="h-pid">PID {real}</span>'), btn)
            self.assertIn(f'name="pid" value="{real}"', row)
            self.assertEqual(row.count('class="h-pid"'), 2)
        self.assertEqual(len(set(heads)), 3)
        self.assertEqual(len(set(confirms)), 3)
        self.assertNotIn("PID 5001", rows[2])  # the mimic can't show the real browser's PID element

    def test_m2_folder_cannot_inject_pid_element(self):
        rows = self.panel([self.g('x<span class="h-pid">PID 1</span>', 7)]).split('<div class="h-br">')[1:]
        self.assertEqual(rows[0].count('<span class="h-pid">'), 2)
        self.assertIn("PID 7", rows[0])

    def test_m2_pid_element_rejects_non_ints(self):
        from botroll.board import _pid_el
        self.assertEqual(_pid_el(True), "")
        self.assertEqual(_pid_el(None), "")
        self.assertEqual(_pid_el("5001"), "")
        self.assertEqual(_pid_el(0), "")
        self.assertEqual(_pid_el(5), ' <span class="h-pid">PID 5</span>')


class Salt(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self)
        self.path = self.home / ".botroll" / "health-salt"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        real = os.path.expanduser
        eu = mock.patch.object(health.os.path, "expanduser",
                               side_effect=lambda p: str(self.home) + p[1:] if p.startswith("~") else real(p))
        eu.start()
        self.addCleanup(eu.stop)
        mock.patch.object(health, "_salt", None).start()
        self.addCleanup(mock.patch.stopall)

    def salt(self):
        health._salt = None
        return health._install_salt()

    def test_t5_created_saved_0600_nonempty(self):
        s = self.salt()
        self.assertEqual(len(s), 32)
        self.assertEqual(self.path.read_bytes(), s)  # actually saved
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.salt(), s)  # stable across restarts
        self.assertTrue(str(self.path).startswith(str(self.home)))

    def test_s1_short_or_empty_file_is_repaired(self):
        for bad in (b"", b"short"):
            self.path.parent.mkdir(exist_ok=True)
            self.path.write_bytes(bad)
            s = self.salt()
            self.assertGreaterEqual(len(s), 16)
            self.assertEqual(self.path.read_bytes(), s)

    def test_s2_loose_mode_tightened(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"k" * 32)
        os.chmod(self.path, 0o644)
        self.assertEqual(self.salt(), b"k" * 32)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_s2_untightenable_file_regenerated(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"k" * 32)
        os.chmod(self.path, 0o644)
        with mock.patch.object(health.os, "fchmod", side_effect=PermissionError):
            s = self.salt()
        self.assertNotEqual(s, b"k" * 32)
        self.assertEqual(self.path.read_bytes(), s)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_s2_symlink_not_followed(self):
        self.path.parent.mkdir()
        target = self.home / "elsewhere"
        target.write_bytes(b"attacker-known-key-0123456789")
        self.path.symlink_to(target)
        s = self.salt()
        self.assertNotEqual(s, b"attacker-known-key-0123456789")
        self.assertEqual(target.read_bytes(), b"attacker-known-key-0123456789")  # target untouched
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(self.path.read_bytes(), s)

    def test_s1_unwritable_falls_back_with_warning(self):
        with mock.patch.object(health.os, "makedirs", side_effect=PermissionError("no")), \
                self.assertLogs("botroll.health", "WARNING") as cm:
            s = self.salt()
        self.assertEqual(len(s), 32)
        self.assertTrue(any("temporary key" in m for m in cm.output))
        self.assertFalse(self.path.exists())

    def test_l2_salt_length_boundary(self):
        self.path.parent.mkdir()
        for n in (0, 1, health.SALT_MIN - 1):
            self.path.write_bytes(b"k" * n)
            s = self.salt()
            self.assertEqual(len(s), 32, n)  # too short: never used as the key, replaced
            self.assertEqual(self.path.read_bytes(), s)
        self.path.write_bytes(b"k" * health.SALT_MIN)
        self.assertEqual(self.salt(), b"k" * health.SALT_MIN)  # long enough: kept as it is

    def test_l2_empty_key_never_used(self):
        import hashlib
        import hmac
        self.path.parent.mkdir()
        self.path.write_bytes(b"")
        health._salt = None
        got = health.profile_id("/some/profile")
        empty = hmac.new(b"", health.norm_path("/some/profile").encode(), hashlib.sha256).hexdigest()[:8]
        self.assertNotEqual(got, empty)

    def test_l2_directory_at_salt_path_is_not_a_key(self):
        self.path.mkdir(parents=True)
        with self.assertLogs("botroll.health", "WARNING"):
            s = self.salt()  # can't replace a folder: a per-process key, with the warning
        self.assertEqual(len(s), 32)
        self.assertTrue(self.path.is_dir())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs FIFOs")
    def test_l2_fifo_with_data_is_not_a_key(self):
        self.path.parent.mkdir()
        os.mkfifo(self.path)
        w = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)  # a writer that has already queued a known key
        self.addCleanup(os.close, w)
        os.write(w, b"attacker-known-key-0123456789ab")
        s = self.salt()
        self.assertNotEqual(s, b"attacker-known-key-0123456789ab")
        self.assertTrue(stat.S_ISREG(os.lstat(self.path).st_mode))  # the FIFO was replaced by a real salt file
        self.assertEqual(self.path.read_bytes(), s)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs FIFOs")
    def test_l1_fifo_without_writer_does_not_hang(self):
        import threading
        self.path.parent.mkdir()
        os.mkfifo(self.path)
        out = []
        t = threading.Thread(target=lambda: out.append(self.salt()), daemon=True)
        t.start()
        t.join(10)
        if t.is_alive():  # blocked in open(): give it a writer so the thread can finish, then fail
            with contextlib.suppress(OSError):
                os.close(os.open(self.path, os.O_WRONLY | os.O_NONBLOCK))
            t.join(5)
            self.fail("opening a FIFO at the salt path blocked")
        self.assertEqual(len(out[0]), 32)
        self.assertTrue(stat.S_ISREG(os.lstat(self.path).st_mode))

    def test_l2_new_salt_folder_is_0700(self):
        self.salt()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_l2_existing_salt_folder_tightened_to_0700(self):
        for salt_exists in (False, True):
            self.path.parent.mkdir(exist_ok=True)
            if salt_exists:
                self.path.write_bytes(b"k" * 32)
                os.chmod(self.path, 0o600)
            os.chmod(self.path.parent, 0o755)
            self.salt()
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700, salt_exists)

    def test_l2_symlinked_salt_folder_left_alone(self):
        target = self.home / "shared"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        self.path.parent.symlink_to(target)
        self.salt()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_no_warning_when_written(self):
        with mock.patch.object(health.log, "warning") as w:
            self.salt()
        w.assert_not_called()


if __name__ == "__main__":
    unittest.main()
