import tmphome  # noqa: F401  (temp HOME before botroll is imported)
import contextlib
import io
import json
import os
import pathlib
import shutil
import smtplib
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from botroll import notify, update
from botroll.notify import EVERY, MAX_WAIT, QUIET, Mailer, Notifier, configure, defang
from botroll.serve import served_snapshot

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples" / "tasks"
BASE = {"BOTROLL_EMAIL_FROM": "BotRoll <bots@example.com>", "BOTROLL_EMAIL_TO": "sam@example.com, ops@example.com"}
T0 = 1_800_000_000.0


class Fake(Mailer):
    def __init__(self, digest_hour=None, fail=0):
        super().__init__("fake", "bots@example.com", ["sam@example.com"], {}, "https://board.example.com", digest_hour)
        self.sent, self.fail = [], fail

    def send(self, mail):
        if self.fail:
            self.fail -= 1
            raise OSError("503 try later")
        self.sent.append(mail)


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data)
        for f in EXAMPLES.glob("*.md"):
            shutil.copy(f, self.data)
        env = mock.patch.dict(os.environ, {"BOTROLL_NOW": "2026-10-10 15:30", "BOTROLL_HUMAN": "sam"})
        env.start()
        self.addCleanup(env.stop)
        self.log = io.StringIO()
        stderr = contextlib.redirect_stderr(self.log)
        stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def notifier(self, mailer):
        return Notifier(self.data, mailer, lambda: served_snapshot(self.data), delays=(0, 0))


class Off(unittest.TestCase):
    def test_off_unless_configured(self):
        self.assertIsNone(configure({}))
        self.assertIsNone(configure({"BOTROLL_EMAIL_PROVIDER": " ", **BASE, "RESEND_API_KEY": "k"}))

    def test_serve_starts_no_notifier_without_email(self):
        from botroll import serve
        env = {"BOTROLL_PASSWORD": "twelve chars", "BOTROLL_SECRET": "s" * 40}
        server = mock.Mock(server_address=("127.0.0.1", 1))
        with mock.patch.object(serve, "make_server", return_value=server), \
                mock.patch.object(notify, "start") as start, mock.patch("sys.stderr"), \
                mock.patch("urllib.request.urlopen") as urlopen, mock.patch("smtplib.SMTP") as smtp:
            serve.serve(serve.configure("/tmp", env), 0)
            start.assert_not_called()
            serve.serve(serve.configure("/tmp", env | BASE | {"BOTROLL_EMAIL_PROVIDER": "resend",
                                                            "RESEND_API_KEY": "k"}), 0)
            start.assert_called_once()
        urlopen.assert_not_called()
        smtp.assert_not_called()

    def test_cli_test_when_off(self):
        from botroll.cli import main
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["email", "test"]), 2)
        self.assertIn("email is off", err.getvalue())

    def test_bad_settings_are_named(self):
        for env, message in [
            ({"BOTROLL_EMAIL_PROVIDER": "sendgrid"}, "one of resend, cloudflare, smtp"),
            ({"BOTROLL_EMAIL_PROVIDER": "resend"}, "BOTROLL_EMAIL_FROM, BOTROLL_EMAIL_TO, RESEND_API_KEY"),
            ({"BOTROLL_EMAIL_PROVIDER": "cloudflare", **BASE, "CF_ACCOUNT_ID": "a"}, "needs CF_EMAIL_TOKEN"),
            ({"BOTROLL_EMAIL_PROVIDER": "resend", **BASE, "RESEND_API_KEY": "k",
              "BOTROLL_EMAIL_TO": "sam@example.com\nBcc: all@example.com"}, "not an email address"),
            ({"BOTROLL_EMAIL_PROVIDER": "smtp", **BASE, "SMTP_HOST": "mail.example.com", "SMTP_TLS": "off"}, "clear"),
            ({"BOTROLL_EMAIL_PROVIDER": "smtp", **BASE, "SMTP_HOST": "h", "SMTP_USER": "u"}, "both SMTP_USER"),
            ({"BOTROLL_EMAIL_PROVIDER": "smtp", **BASE, "SMTP_HOST": "h", "SMTP_TLS": "maybe"}, "SMTP_TLS"),
            ({"BOTROLL_EMAIL_PROVIDER": "resend", **BASE, "RESEND_API_KEY": "k", "BOTROLL_URL": "http://x.com"}, "https"),
            ({"BOTROLL_EMAIL_PROVIDER": "resend", **BASE, "RESEND_API_KEY": "k", "BOTROLL_DIGEST_HOUR": "24"}, "0 to 23"),
            ({"BOTROLL_EMAIL_PROVIDER": "resend", **BASE, "RESEND_API_KEY": "k", "BOTROLL_TZ": "Mars/Base"}, "timezone"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                configure(env)
        m = configure({"BOTROLL_EMAIL_PROVIDER": "Resend", **BASE, "RESEND_API_KEY": "k", "BOTROLL_DIGEST_HOUR": "8",
                       "BOTROLL_URL": "https://board.example.com", "BOTROLL_TZ": "Europe/London"})
        self.assertEqual((m.provider, m.to, m.digest_hour), ("resend", ["sam@example.com", "ops@example.com"], 8))


class NeedsYou(Base):
    def test_debounced_and_batched(self):
        mailer = Fake()
        n = self.notifier(mailer)
        n.tick(T0)
        n.tick(T0 + QUIET - 1)
        self.assertEqual(mailer.sent, [])  # waiting for the burst to settle
        n.tick(T0 + QUIET)
        [first] = mailer.sent
        self.assertEqual(first.subject, "BotRoll: 4 new things need you")
        self.assertIn("Sasha · Social · T-31", first.text)
        self.assertLess(first.text.index("T-31"), first.text.index("T-7"))  # oldest first, as on the board

        update(self.data, "builder", "T-41", "waiting", "Merge without the fixtures?")
        n.tick(T0 + 120)
        update(self.data, "qa", "T-23", "waiting", "Is Safari 17 enough?")
        n.tick(T0 + 300)
        n.tick(T0 + QUIET + EVERY - 1)
        self.assertEqual(len(mailer.sent), 1)  # at most one email per 15 minutes
        n.tick(T0 + QUIET + EVERY)
        second = mailer.sent[1]
        self.assertEqual(second.subject, "BotRoll: 2 new things need you")
        self.assertIn("2 new things need you. 6 are waiting in all.", second.text)
        self.assertLess(second.text.index("Max · Engineering · T-41"), second.text.index("Kai · QA · T-23"))
        self.assertNotIn("T-31", second.text)
        n.tick(T0 + 10 * EVERY)
        self.assertEqual(len(mailer.sent), 2)

    def test_answered_before_sending_is_not_sent(self):
        mailer = Fake()
        n = self.notifier(mailer)
        n.tick(T0)
        n.tick(T0 + QUIET)
        update(self.data, "qa", "T-23", "waiting", "Safari 17?")
        n.tick(T0 + 100)
        update(self.data, "qa", "T-23", "doing")
        n.tick(T0 + QUIET + EVERY)
        self.assertEqual(len(mailer.sent), 1)

    def test_asking_again_is_new_again(self):
        mailer = Fake()
        n = self.notifier(mailer)
        n.tick(T0)
        n.tick(T0 + QUIET)
        update(self.data, "social", "T-31", "doing")
        n.tick(T0 + 100)
        update(self.data, "social", "T-31", "waiting", "One more look?")
        n.tick(T0 + 200)
        n.tick(T0 + QUIET + EVERY)
        self.assertEqual(mailer.sent[1].subject, "BotRoll: 1 new thing needs you")
        self.assertIn("One more look?", mailer.sent[1].text)

    def test_no_resend_after_restart(self):
        mailer = Fake()
        self.notifier(mailer).tick(T0)
        self.notifier(mailer).tick(T0 + QUIET)  # pending asks survive a restart
        self.assertEqual(len(mailer.sent), 1)
        for later in (T0 + QUIET + 1, T0 + 2 * EVERY, T0 + 100 * EVERY):
            self.notifier(mailer).tick(later)
        self.assertEqual(len(mailer.sent), 1)
        state = json.loads(pathlib.Path(self.data, ".notify.json").read_text())
        self.assertEqual(len(state["notified"]), 4)
        self.assertNotIn(".notify", " ".join(b["bot"] for b in served_snapshot(self.data)["bots"]))

    def test_retries_then_backs_off_and_logs(self):
        mailer = Fake(fail=3)  # one send tries three times
        n = self.notifier(mailer)
        n.tick(T0)
        n.tick(T0 + QUIET)
        self.assertEqual((mailer.sent, mailer.fail), ([], 0))
        self.assertIn("couldn't send 'BotRoll: 4 new things need you' via fake: 503 try later. Trying again in 2m.",
                      self.log.getvalue())
        mailer.fail = 3
        n.tick(T0 + QUIET + 119)
        self.assertEqual(mailer.fail, 3)  # still backing off
        n.tick(T0 + QUIET + 120)
        self.assertIn("Trying again in 4m.", self.log.getvalue())
        n.tick(T0 + QUIET + 120 + 240)
        self.assertEqual(len(mailer.sent), 1)
        self.assertEqual((n.state["failures"], n.state["retry_at"]), (0, 0))

    def test_a_crashing_check_never_stops_the_loop(self):
        n = self.notifier(Fake())
        calls = []

        def tick(at):
            calls.append(at)
            raise RuntimeError("boom")
        n.tick = tick
        stop = notify.start(n, every=0.01)
        self.addCleanup(stop.set)
        deadline = time.time() + 5
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(calls), 3)  # it kept going after the first crash
        self.assertIn("skipped a check: RuntimeError('boom')", self.log.getvalue())

    def test_trickle_sends_within_max_wait(self):
        mailer = Fake()
        n = self.notifier(mailer)
        n.state["notified"] = ["assistant/T-7", "assistant/T-8", "researcher/T-15", "social/T-31"]
        update(self.data, "builder", "T-41", "waiting", "first")
        n.tick(T0)
        update(self.data, "qa", "T-23", "waiting", "again")
        n.tick(T0 + QUIET - 1)  # a new ask reset the quiet window
        self.assertEqual(mailer.sent, [])
        n.tick(T0 + MAX_WAIT)
        self.assertEqual(len(mailer.sent), 1)
        self.assertIn("2 new things need you", mailer.sent[0].subject)

    def test_bad_notify_json_is_moved_aside(self):
        path = pathlib.Path(self.data, ".notify.json")
        for raw in ("[]", "42", '{"notified": 1}', '{"pending": []}',
                    '{"sent_at": NaN}', '{"retry_at": Infinity}',
                    '{"pending": {"a/T-1": Infinity}}',
                    '{"sent_at": 1e20}'):
            path.write_text(raw)
            n = self.notifier(Fake())
            self.assertEqual(n.state["notified"], [])
            self.assertEqual(n.state["pending"], {})
            bad = list(pathlib.Path(self.data).glob(".notify.json.bad-*"))
            self.assertTrue(bad, raw)
            for f in bad:
                f.unlink()
        self.assertIn("ignored a bad .notify.json", self.log.getvalue())


class Digest(Base):
    def setUp(self):
        super().setUp()
        tz = mock.patch.dict(os.environ, {"BOTROLL_TZ": "America/New_York"})
        tz.start()
        self.addCleanup(tz.stop)
        self.at = datetime(2026, 10, 10, 18, 5, tzinfo=ZoneInfo("America/New_York")).timestamp()

    def test_once_a_day_at_the_hour_in_botroll_tz(self):
        mailer = Fake(digest_hour=18)
        n = self.notifier(mailer)
        n.state["notified"] = ["assistant/T-7", "assistant/T-8", "researcher/T-15", "social/T-31"]
        n.tick(self.at - 3600)
        self.assertEqual(mailer.sent, [])
        n.tick(self.at)
        [mail] = mailer.sent
        self.assertEqual(mail.subject, "BotRoll daily: 7 done, 3 stuck")
        self.assertTrue(mail.text.startswith("7 done in the last 24h, 3 stuck and 1 gone quiet.\n"))
        for text in ["Done today", "Fix login redirect loop", "Stuck", "Deploy preview build for review",
                     "blocked: needs qa/T-22", "Gone quiet", "Max"]:
            self.assertIn(text, mail.text)
        self.assertNotIn("Watchdog", mail.text)
        n.tick(self.at + 600)
        self.notifier(mailer).tick(self.at + 1200)
        self.assertEqual(len(mailer.sent), 1)
        self.notifier(mailer).tick(self.at + 86400)
        self.assertEqual(len(mailer.sent), 2)

    def test_watchdog_findings(self):
        from botroll.serve import store_findings
        store_findings(self.data, [{"kind": "capture", "text": "pid 9 (ffmpeg) capturing for 2h, not on any open item",
                                    "bot": None, "id": None, "pid": 9}])
        mailer = Fake(digest_hour=18)
        n = self.notifier(mailer)
        n.tick(self.at)
        digest = next(m for m in mailer.sent if m.subject.startswith("BotRoll daily"))
        self.assertIn("Watchdog\n- pid 9 (ffmpeg) capturing for 2h", digest.text)
        self.assertIn("and 1 watchdog finding.", digest.text)

    def test_skipped_when_empty(self):
        for f in pathlib.Path(self.data).glob("*.md"):
            f.unlink()
        mailer = Fake(digest_hour=18)
        n = self.notifier(mailer)
        n.tick(self.at)
        self.assertEqual(mailer.sent, [])
        self.assertEqual(n.state["digest_on"], "2026-10-10")


class Escaping(Base):
    def test_defang(self):
        for raw, safe in [
            ("see https://evil.example/login now", "see https[://]evil[.]example/login now"),
            ("mail sam@bank.com or www.bank.com", "mail sam@bank[.]com or www[.]bank[.]com"),
            ("drafts/thread.md v0.9.2 e.g. 3.5", "drafts/thread[.]md v0.9.2 e.g. 3.5"),
            ("two\r\nlines\tand \u202egnp.exe and zero\u200bwidth", "two lines and gnp[.]exe and zerowidth"),
            ("visit \u0430pple.com now", "visit \u0430pple[.]com now"),  # Cyrillic а
            ("see example\uff0ecom and example\u3002com", "see example[.]com and example[.]com"),
            ("reset at 203.0.113.9/login", "reset at 203[.]0[.]113[.]9/login"),
            ("host 2001:db8::1 and [::1]", "host 2001[:]db8[:][:]1 and [[:][:]1]"),
            ("open 0x7f000001 or 2130706433", "open 0x[.]7f000001 or 21307064[.]33"),
            ("go to 0x7f.0.0.1/login", "go to 0x7f[.]0[.]0[.]1/login"),
            ("also 0x7f.0x0.0x0.0x1", "also 0x7f[.]0x0[.]0x0[.]0x1"),
        ]:
            self.assertEqual(defang(raw), safe)
        self.assertEqual(len(defang("x" * 1000)), 300)

    def test_bot_text_cannot_phish(self):
        pathlib.Path(self.data, "evil.md").write_text(
            "---\nname: IT <b>Support</b> paypal.com\nrole: Security\n---\n"
            '- [waiting-sam] T-1 <a href="https://evil.example/reset">Reset your password</a> · asked 2026-10-10 15:00 · '
            "updated 2026-10-10 15:00 · note: Your account is locked. Go to https://evil.example/unlock\u202e today\n")
        mailer = Fake()
        n = self.notifier(mailer)
        n.tick(T0)
        n.tick(T0 + QUIET)
        [mail] = mailer.sent
        self.assertEqual(mail.subject, "BotRoll: 5 new things need you")
        for body in (mail.text, mail.html):
            self.assertNotIn("https://evil", body)
            self.assertNotIn("evil.example", body)
            self.assertNotIn("paypal.com", body)
            self.assertNotIn("\u202e", body)
        self.assertIn("https[://]evil[.]example/unlock today", mail.text)
        self.assertNotIn("<a href=\"https://evil", mail.html)
        self.assertNotIn("<b>Support", mail.html)
        self.assertIn("&lt;b&gt;Support&lt;/b&gt;", mail.html)
        self.assertEqual(mail.html.count("<a "), 1)
        self.assertIn('href="https://board.example.com"', mail.html)
        self.assertIn("Open the board: https://board.example.com", mail.text)

    def test_without_botroll_url_there_are_no_links(self):
        mail = notify.needs_you_mail([], 0, None)
        self.assertNotIn("href", mail.html)
        self.assertNotIn("Open the board", mail.text)


class Providers(unittest.TestCase):
    mail = notify.Mail("BotRoll: test", "plain", "<p>html</p>")

    def mailer(self, provider, **env):
        return configure({"BOTROLL_EMAIL_PROVIDER": provider, **BASE, **env})

    def post(self, mailer, reply=b'{"id": "x"}'):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = reply
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            mailer.send(self.mail)
        [[req], kwargs] = urlopen.call_args
        return req, json.loads(req.data), kwargs

    def test_resend(self):
        req, body, kwargs = self.post(self.mailer("resend", RESEND_API_KEY="re_123"))
        self.assertEqual((req.full_url, req.get_method()), ("https://api.resend.com/emails", "POST"))
        self.assertEqual(req.get_header("Authorization"), "Bearer re_123")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertTrue(req.get_header("User-agent").startswith("botroll/"))
        self.assertEqual(body, {"from": "BotRoll <bots@example.com>", "to": ["sam@example.com", "ops@example.com"],
                                "subject": "BotRoll: test", "text": "plain", "html": "<p>html</p>"})
        self.assertEqual(kwargs, {"timeout": 30})

    def test_cloudflare(self):
        mailer = self.mailer("cloudflare", CF_ACCOUNT_ID="acc1", CF_EMAIL_TOKEN="cf_tok")
        ok = b'{"success": true, "errors": [], "result": {"delivered": ["sam@example.com"], "permanent_bounces": []}}'
        req, body, _ = self.post(mailer, ok)
        self.assertEqual(req.full_url, "https://api.cloudflare.com/client/v4/accounts/acc1/email/sending/send")
        self.assertEqual(req.get_header("Authorization"), "Bearer cf_tok")
        self.assertEqual(body, {"from": "BotRoll <bots@example.com>", "to": ["sam@example.com", "ops@example.com"],
                                "subject": "BotRoll: test", "text": "plain", "html": "<p>html</p>"})
        for reply in [b'{"success": false, "errors": [{"code": 10001}]}',
                      b'{"success": true, "result": {"permanent_bounces": ["sam@example.com"]}}']:
            with self.subTest(reply=reply), self.assertRaisesRegex(OSError, "didn't deliver"):
                self.post(mailer, reply)

    def test_http_errors_become_os_errors(self):
        error = urllib.error.HTTPError("https://api.resend.com/emails", 422, "bad", {}, io.BytesIO(b'{"message": "no"}'))
        with mock.patch("urllib.request.urlopen", side_effect=error), self.assertRaisesRegex(OSError, "422"):
            self.mailer("resend", RESEND_API_KEY="k").send(self.mail)

    def smtp(self, cls, **env):
        with mock.patch(f"smtplib.{cls}") as connect:
            self.mailer("smtp", SMTP_HOST=env.pop("host", "smtp.example.com"), **env).send(self.mail)
        return connect

    def test_smtp_starttls(self):
        connect = self.smtp("SMTP", SMTP_USER="u", SMTP_PASS="p")
        self.assertEqual(connect.call_args[0], ("smtp.example.com", 587))
        s = connect.return_value.__enter__.return_value
        s.starttls.assert_called_once()
        s.login.assert_called_once_with("u", "p")
        [[msg], _] = s.send_message.call_args
        self.assertEqual((msg["Subject"], msg["From"], msg["To"]),
                         ("BotRoll: test", "BotRoll <bots@example.com>", "sam@example.com, ops@example.com"))
        self.assertEqual([p.get_content_type() for p in msg.iter_parts()], ["text/plain", "text/html"])
        self.assertEqual(msg.get_body(("html",)).get_content().strip(), "<p>html</p>")

    def test_smtp_ssl_and_local_plain(self):
        connect = self.smtp("SMTP_SSL", SMTP_TLS="ssl", SMTP_PORT="2465")
        self.assertEqual(connect.call_args[0], ("smtp.example.com", 2465))
        connect.return_value.__enter__.return_value.login.assert_not_called()
        connect = self.smtp("SMTP", SMTP_TLS="off", host="localhost")
        self.assertEqual(connect.call_args[0], ("localhost", 25))
        connect.return_value.__enter__.return_value.starttls.assert_not_called()

    def test_cli_sends_a_test_email(self):
        from botroll.cli import main
        env = {"BOTROLL_EMAIL_PROVIDER": "smtp", **BASE, "SMTP_HOST": "smtp.example.com"}
        with mock.patch.dict(os.environ, env), mock.patch("smtplib.SMTP") as connect, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["email", "test"]), 0)
        self.assertEqual(out.getvalue(), "sent a test email to sam@example.com, ops@example.com via smtp\n")
        [[msg], _] = connect.return_value.__enter__.return_value.send_message.call_args
        self.assertEqual(msg["Subject"], "BotRoll: test email")
        with mock.patch.dict(os.environ, env), \
                mock.patch("smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "busy")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["email", "test"]), 2)
        self.assertIn("smtp didn't take it", err.getvalue())


if __name__ == "__main__":
    unittest.main()
