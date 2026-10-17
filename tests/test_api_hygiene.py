"""Optional API hardening: Permissions-Policy, __Host- cookies, a separate health token, a GET that changes
nothing, and push --prune --dry-run. Every one of them is off (or unchanged) by default."""
import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import contextlib
import io
import json
import os
import pathlib
import unittest
from unittest import mock

import test_serve
from test_serve import HASH, PASSWORD, SECRET, TOKEN

from botroll import health, serve

HEALTH_TOKEN = "h" * 40


class PermissionsPolicy(test_serve.Server):
    def test_on_every_response(self):
        for r in (self.request("GET", "/health"), self.request("GET", "/login"), self.request("GET", "/"),
                  self.request("POST", "/api/roll/x", b"", {"Content-Length": "0"})):
            policy = r.headers["Permissions-Policy"]
            self.assertIsNotNone(policy)
            for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()", "usb=()"):
                self.assertIn(feature, policy)


class HostCookies(test_serve.Server):
    def test_off_by_default(self):
        names = set(self.request("GET", "/login").cookies())
        self.assertEqual(names, {"botroll_csrf"})

    def test_prefixed_and_still_signs_in(self):
        self.cfg.host_cookies = True
        r = self.request("GET", "/login")
        self.assertEqual(set(r.cookies()), {"__Host-botroll_csrf"})
        for c in r.headers.get_all("Set-Cookie"):
            self.assertIn("Secure", c)
            self.assertIn("Path=/", c)
            self.assertNotIn("Domain", c)
        r, cookies = self.sign_in()
        self.assertEqual(r.status, 303)
        session = {**cookies, **r.cookies()}
        self.assertIn("__Host-botroll_session", session)
        self.assertEqual(self.request("GET", "/", cookies=session).status, 200)
        plain = {k.removeprefix("__Host-"): v for k, v in session.items()}
        self.assertEqual(self.request("GET", "/", cookies=plain).status, 303)  # unprefixed names are ignored

    def test_needs_secure_cookies(self):
        env = {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET, "BOTROLL_HOST_COOKIES": "1"}
        self.assertTrue(serve.configure(self.data, env).host_cookies)
        for kw in ({"insecure_http": True}, {"insecure_local": True}):
            with self.assertRaisesRegex(ValueError, "BOTROLL_HOST_COOKIES needs Secure"):
                serve.configure(self.data, env, **kw)


class HealthToken(test_serve.Server):
    def setUp(self):
        super().setUp()
        self.cfg.health = True

    def snap(self):
        import time
        from test_health import sample_at
        return json.dumps(sample_at(int(time.time()))).encode()

    def test_unset_push_token_still_works_everywhere(self):
        self.assertEqual(self.push("/api/health", self.snap()).status, 200)
        self.assertEqual(self.push("/api/roll/ann", b"- [todo] T-1 x\n").status, 200)

    def test_set_it_is_the_only_token_for_health(self):
        self.cfg.health_token, self.cfg.health_control = HEALTH_TOKEN, True
        self.assertEqual(self.push("/api/health", self.snap()).status, 401)  # push token refused here
        self.assertEqual(self.push("/api/health", self.snap(), token=HEALTH_TOKEN).status, 200)
        self.assertEqual(self.request("GET", "/api/health/commands",
                                      headers={"Authorization": f"Bearer {TOKEN}"}).status, 401)
        self.assertEqual(self.request("GET", "/api/health/commands",
                                      headers={"Authorization": f"Bearer {HEALTH_TOKEN}"}).status, 200)
        self.assertEqual(self.push("/api/health/commands/result", b'{"id": "x", "status": "ok"}').status, 401)
        # and it opens nothing else
        for route in ("/api/roll/ann", "/api/watch", "/api/prune", "/api/healthz"):
            self.assertEqual(self.push(route, b"[]", token=HEALTH_TOKEN).status, 401, route)
        self.assertEqual(self.push("/api/roll/ann", b"- [todo] T-1 x\n").status, 200)

    def test_peek_reserves_slots_for_the_right_token_only(self):
        self.cfg.health_token = HEALTH_TOKEN

        class Peek:
            def __init__(self, raw):
                self.raw = raw

            def gettimeout(self):
                return None

            def settimeout(self, t):
                pass

            def recv(self, n, flags):
                return self.raw

        def kind(path, token):
            return serve._peek_kind(Peek(f"POST {path} HTTP/1.1\r\nAuthorization: Bearer {token}\r\n\r\n".encode()),
                                    self.cfg)
        self.assertEqual(kind("/api/health", HEALTH_TOKEN), "auth")
        self.assertEqual(kind("/api/health", TOKEN), "unknown")
        self.assertEqual(kind("/api/roll/ann", TOKEN), "auth")
        self.assertEqual(kind("/api/roll/ann", HEALTH_TOKEN), "unknown")

    def test_configure_checks_it(self):
        base = {"BOTROLL_PASSWORD_HASH": HASH, "BOTROLL_SECRET": SECRET, "BOTROLL_PUSH_TOKEN": TOKEN}
        self.assertIsNone(serve.configure(self.data, base).health_token)
        self.assertEqual(serve.configure(self.data, {**base, "BOTROLL_HEALTH_TOKEN": HEALTH_TOKEN}).health_token,
                         HEALTH_TOKEN)
        for bad, why in (("short", "at least 32"), (TOKEN, "different"), (SECRET, "different")):
            with self.assertRaisesRegex(ValueError, why):
                serve.configure(self.data, {**base, "BOTROLL_HEALTH_TOKEN": bad})
        with self.assertRaisesRegex(ValueError, "different"):
            long_password = PASSWORD + " and some more words"  # 32+ characters, so only the match fails
            serve.configure(self.data, {"BOTROLL_PASSWORD": long_password, "BOTROLL_SECRET": SECRET,
                                        "BOTROLL_HEALTH_TOKEN": long_password})

    def test_collector_prefers_it(self):
        from botroll.cli import main
        for env, want in (({"BOTROLL_HEALTH_TOKEN": HEALTH_TOKEN, "BOTROLL_PUSH_TOKEN": TOKEN}, HEALTH_TOKEN),
                          ({"BOTROLL_PUSH_TOKEN": TOKEN}, TOKEN)):
            clean = {k: v for k, v in os.environ.items() if not k.startswith("BOTROLL_")}
            with mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                    mock.patch.object(health, "sample", return_value={"ts": 1}), \
                    mock.patch.object(health, "push") as push:
                self.assertEqual(main(["--dir", self.data, "health", "--url", "https://board.example"]), 0)
            self.assertEqual(push.call_args.args[:2], ("https://board.example", want))


class CommandsGetIsReadOnly(test_serve.Server):
    def test_get_with_control_off_changes_nothing(self):
        health.enqueue_command(self.data, "stop-helper", {"pid": 4243, "started_at": 1001})
        path = pathlib.Path(self.data, health.COMMANDS)
        before = path.read_bytes()
        for cfg in ((False, False), (True, False), (False, True)):
            self.cfg.health, self.cfg.health_control = cfg
            r = self.request("GET", "/api/health/commands", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(r.status, 404)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(health.pending_commands(self.data)), 1)

    def test_startup_clears_them_instead(self):
        health.enqueue_command(self.data, "stop-helper", {"pid": 4243, "started_at": 1001})
        self.cfg.health, self.cfg.health_control = True, True
        self.assertEqual(serve.clear_stale(self.cfg), 0)  # control on: queued commands are kept
        self.assertEqual(len(health.pending_commands(self.data)), 1)
        self.cfg.health_control = False
        self.assertEqual(serve.clear_stale(self.cfg), 1)
        self.assertEqual(health.pending_commands(self.data), [])
        health.enqueue_command(self.data, "stop-helper", {"pid": 4244, "started_at": 1002})
        self.cfg.health, self.cfg.health_control = False, True  # control on but health off: still off
        self.assertEqual(serve.clear_stale(self.cfg), 1)

    def test_serve_calls_it_at_startup(self):
        with mock.patch.object(serve, "ensure_data"), mock.patch.object(serve, "clear_stale") as clear, \
                mock.patch.object(serve, "make_server", side_effect=KeyboardInterrupt), \
                contextlib.suppress(KeyboardInterrupt):
            serve.serve(self.cfg, 0)
        clear.assert_called_once_with(self.cfg)


class PruneDryRun(unittest.TestCase):
    env = {"BOTROLL_PUSH_URL": "https://push.example", "BOTROLL_PUSH_TOKEN": "t" * 40}

    def run_cli(self, args, would=("old",)):
        from botroll.cli import main

        def fake(url, token, d, prune=False, removed=None, confirm=False):
            removed.extend(would)
            return (1, 0, 0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch("botroll.serve.push", side_effect=fake) as push, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
        return code, push, out.getvalue(), err.getvalue()

    def test_warns_and_exits_3_when_something_would_go(self):
        code, push, out, err = self.run_cli(["push", "--prune", "--dry-run"], ("old", "gone"))
        self.assertEqual(code, 3)
        self.assertFalse(push.call_args.kwargs["confirm"])  # never deletes
        self.assertIn("would prune (dry run; add --yes to delete) 2: old, gone", out)
        self.assertIn("botroll: warning: --prune --yes would remove 2 roll files from the server: old, gone", err)

    def test_quiet_and_0_when_nothing_would(self):
        code, _, _, err = self.run_cli(["push", "--prune", "--dry-run"], ())
        self.assertEqual((code, err), (0, ""))

    def test_default_prune_unchanged(self):
        code, push, _, err = self.run_cli(["push", "--prune"])
        self.assertEqual((code, err, push.call_args.kwargs["confirm"]), (0, "", False))

    def test_refuses_nonsense(self):
        for args, why in ((["push", "--dry-run"], "--dry-run only goes with --prune"),
                          (["push", "--prune", "--yes", "--dry-run"], "can't go together")):
            code, push, _, err = self.run_cli(args)
            self.assertEqual(code, 2)
            self.assertIn(why, err)
            push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
