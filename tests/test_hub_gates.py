"""Regression tests for hub login lockout, vault password, and card opens.

These paths decide whether a wrong password locks the gate, whether the NSFW
volume password is required, and which local trees a card is allowed to open.
"""
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SERVER = Path(__file__).resolve().parents[1] / "hub" / "server"
sys.path.insert(0, str(SERVER))

import hub_extras  # noqa: E402
import hub_server  # noqa: E402


class _Post(hub_server.HubHandler):
    def __init__(self, path, raw, headers):
        # Skip BaseHTTPRequestHandler.__init__; it would read a live socket.
        self.path = path
        self.command = "POST"
        self.headers = headers
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []
        self.requestline = ""
        self.request_version = "HTTP/1.1"
        self.close_connection = True

    def is_https(self):
        return False

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, fmt, *args):
        return


class _Get:
    def __init__(self, path):
        self.path = path
        self.command = "GET"
        self.headers = {}
        self.errors = []

    def is_https(self):
        return True

    def send_error(self, code, message=None, explain=None):
        self.errors.append(code)


class HubGatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.org = base / "org"
        self.board = base / "board"
        self.root = base / "hubroot"
        self.vault = self.org / "Secure" / "NSFW-Vault"
        self.cloud = base / "cloud-backup"
        self.sec = base / "secure" / "security.db"
        self.main_sec = base / "main" / "security.db"
        self.sessions = base / "secure" / "sessions.db"
        for path in (self.home, self.org, self.board, self.root, self.cloud, self.sec.parent, self.main_sec.parent):
            path.mkdir(parents=True, exist_ok=True)

        self._prev = {
            "HOME": hub_server.HOME,
            "ORG": hub_server.ORG,
            "BOARD": hub_server.BOARD,
            "ROOT": hub_server.ROOT,
            "VAULT": hub_server.VAULT,
            "SPARSE": hub_server.SPARSE,
            "FUN_VOL_CANDIDATES": list(hub_server.FUN_VOL_CANDIDATES),
            "CLOUD_BACKUP": hub_server.CLOUD_BACKUP,
            "SEC_DB": hub_server.SEC_DB,
            "MAIN_SEC": hub_server.MAIN_SEC,
            "SESSIONS_DB": hub_server.SESSIONS_DB,
            "extras_home": hub_extras.HOME,
            "extras_sec": hub_extras.HUB_SEC_DB,
            "fails": dict(hub_server.FAILS),
            "sessions": dict(hub_server.SESSIONS),
        }
        hub_server.HOME = self.home
        hub_server.ORG = self.org
        hub_server.BOARD = self.board
        hub_server.ROOT = self.root
        hub_server.VAULT = self.vault
        hub_server.SPARSE = base / "missing.sparsebundle"
        hub_server.FUN_VOL_CANDIDATES = [base / "no-volume"]
        hub_server.CLOUD_BACKUP = self.cloud
        hub_server.SEC_DB = self.sec
        hub_server.MAIN_SEC = self.main_sec
        hub_server.SESSIONS_DB = self.sessions
        hub_extras.HOME = self.home
        hub_extras.HUB_SEC_DB = self.sec
        hub_server.SESSIONS.clear()
        hub_server.FAILS["n"] = 0
        hub_server.FAILS["until"] = 0.0

    def tearDown(self):
        hub_server.HOME = self._prev["HOME"]
        hub_server.ORG = self._prev["ORG"]
        hub_server.BOARD = self._prev["BOARD"]
        hub_server.ROOT = self._prev["ROOT"]
        hub_server.VAULT = self._prev["VAULT"]
        hub_server.SPARSE = self._prev["SPARSE"]
        hub_server.FUN_VOL_CANDIDATES = self._prev["FUN_VOL_CANDIDATES"]
        hub_server.CLOUD_BACKUP = self._prev["CLOUD_BACKUP"]
        hub_server.SEC_DB = self._prev["SEC_DB"]
        hub_server.MAIN_SEC = self._prev["MAIN_SEC"]
        hub_server.SESSIONS_DB = self._prev["SESSIONS_DB"]
        hub_extras.HOME = self._prev["extras_home"]
        hub_extras.HUB_SEC_DB = self._prev["extras_sec"]
        hub_server.FAILS.clear()
        hub_server.FAILS.update(self._prev["fails"])
        hub_server.SESSIONS.clear()
        hub_server.SESSIONS.update(self._prev["sessions"])
        self.tmp.cleanup()

    def post(self, path, payload, cookie=None, content_type="application/json"):
        if isinstance(payload, bytes):
            raw = payload
        else:
            raw = payload.encode()
        headers = {"Content-Type": content_type, "Content-Length": str(len(raw))}
        if cookie:
            headers["Cookie"] = cookie
        stub = _Post(path, raw, headers)
        stub.do_POST()
        return stub

    def auth_cookie(self):
        return "%s=%s" % (hub_server.COOKIE, hub_server.new_session())

    def body(self, stub):
        return json.loads(stub.wfile.getvalue().decode())

    def secrets(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE secrets (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.executemany("INSERT INTO secrets(key, value) VALUES(?, ?)", rows)
        con.commit()
        con.close()

    def test_login_locks_after_five_failures_and_success_clears_the_counter(self):
        created = hub_extras.create_hub_account("Robert", "correct-password", "local")
        self.assertTrue(created["ok"])

        for _ in range(4):
            bad = self.post("/api/login", json.dumps({"password": "nope"}))
            self.assertEqual(bad.status, 401)
        self.assertEqual(hub_server.FAILS["n"], 4)
        self.assertEqual(hub_server.FAILS["until"], 0.0)

        fifth = self.post("/api/login", json.dumps({"password": "nope"}))
        self.assertEqual(fifth.status, 401)
        self.assertEqual(hub_server.FAILS["n"], 0)
        self.assertGreater(hub_server.FAILS["until"], time.time())

        before = len(hub_server.SESSIONS)
        locked = self.post("/api/login", json.dumps({"password": "correct-password"}))
        self.assertEqual(locked.status, 429)
        self.assertEqual(len(hub_server.SESSIONS), before)
        self.assertNotIn("Set-Cookie", [key for key, _ in locked.sent_headers])

        hub_server.FAILS["until"] = 0.0
        ok = self.post("/api/login", json.dumps({"password": "correct-password"}))
        self.assertEqual(ok.status, 200)
        self.assertEqual(hub_server.FAILS["n"], 0)
        cookie = [value for key, value in ok.sent_headers if key == "Set-Cookie"]
        self.assertEqual(len(cookie), 1)
        self.assertIn("HttpOnly", cookie[0])
        self.assertIn("SameSite=Lax", cookie[0])
        self.assertNotIn("Secure", cookie[0])

        for _ in range(4):
            again = self.post("/api/login", json.dumps({"password": "nope"}))
            self.assertEqual(again.status, 401)
        self.assertEqual(hub_server.FAILS["until"], 0.0)

    def test_session_cookie_is_secure_only_on_https(self):
        class Jar:
            def __init__(self, https):
                self._https = https
                self.sent = []

            def is_https(self):
                return self._https

            def send_header(self, key, value):
                self.sent.append((key, value))

        plain = Jar(False)
        hub_server.HubHandler.set_session_cookie(plain, "tok")
        hub_server.HubHandler.clear_session_cookie(plain)
        secure = Jar(True)
        hub_server.HubHandler.set_session_cookie(secure, "tok")

        self.assertIn("HttpOnly", plain.sent[0][1])
        self.assertNotIn("Secure", plain.sent[0][1])
        self.assertIn("Max-Age=0", plain.sent[1][1])
        self.assertNotIn("Secure", plain.sent[1][1])
        self.assertIn("Secure", secure.sent[0][1])
        self.assertIn("hub_sess=tok", secure.sent[0][1])

    def test_vault_password_fails_closed_and_prefers_the_hub_db(self):
        with self.assertRaises(RuntimeError) as missing:
            hub_server.get_nsfw_password()
        self.assertIn("vault_password", str(missing.exception))

        self.secrets(self.sec, [("vault_password", "")])
        with self.assertRaises(RuntimeError):
            hub_server.get_nsfw_password()

        self.secrets(self.main_sec, [("vault_password", "from-main")])
        self.assertEqual(hub_server.get_nsfw_password(), "from-main")

        self.sec.unlink()
        self.secrets(self.sec, [("vault_password", "from-hub")])
        self.assertEqual(hub_server.get_nsfw_password(), "from-hub")

    def test_card_paths_resolve_aliases_and_keep_the_vault_off_the_default_map(self):
        self.assertEqual(
            hub_server.card_paths("Pacific", "MIDI"),
            [self.board / "Pacific" / "MIDI"],
        )
        self.assertEqual(
            hub_server.card_paths("park-place", "cos_mirror"),
            [self.board / "Park-Place" / "CoS-Mirror"],
        )
        self.assertEqual(
            hub_server.card_paths("vermont", "cloud-backup"),
            [self.board / "Vermont" / "CloudBackup"],
        )
        self.assertEqual(
            hub_server.card_paths("baltic", "medical-records"),
            [self.board / "Baltic" / "Medical-Records"],
        )
        self.assertEqual(hub_server.card_paths("pacific", "not-a-sub"), [])
        self.assertEqual(hub_server.card_paths("free-parking"), [])
        self.assertEqual(hub_server.card_paths("ventnor"), [])
        self.assertEqual(hub_server.card_paths("nsfw", "nsfw-vault"), [])

        medical = hub_server.card_paths("baltic-db")
        self.assertEqual(medical[0], self.home / "Documents" / "MainDB" / "medical_records.db")
        vermont = hub_server.card_paths("vermont")
        self.assertIn(self.cloud, vermont)
        for path in medical + vermont + hub_server.card_paths("VENTNOR", "NSFW-Vault"):
            self.assertTrue(str(path).startswith(str(self.tmp.name)))

    def test_secret_filenames_are_not_served(self):
        for path in (
            "/x/Security.DB",
            "/export/passwords.csv",
            "/tmp/chrome-import-passwords.txt",
            "/secure/readme.md",
            "/notes/vault_password.txt",
        ):
            got = _Get(path)
            hub_server.HubHandler.do_GET(got)
            self.assertEqual(got.errors, [404], path)

    def test_open_endpoints_require_a_session_and_refuse_the_vault_sub(self):
        with patch("hub_server.subprocess.run") as run:
            anon = self.post("/api/open-nsfw", b"{}")
            self.assertEqual(anon.status, 401)
            run.assert_not_called()

            cookie = self.auth_cookie()
            locked = self.post("/api/open-nsfw", b"{}", cookie=cookie)
            self.assertEqual(locked.status, 200)
            self.assertFalse(self.body(locked)["ok"])
            self.assertIn("vault_password", self.body(locked)["error"])
            self.assertFalse(self.vault.exists())
            run.assert_not_called()

            for path, payload in (
                ("/api/open-card", {"card": "ventnor", "sub": "nsfw-vault"}),
                ("/api/open-card", {"card": "nsfw", "sub": "NSFW-Vault"}),
                ("/api/open-sub", {"card": "ventnor", "sub": "nsfw-vault"}),
            ):
                refused = self.post(path, json.dumps(payload), cookie=cookie)
                self.assertEqual(refused.status, 400, payload)
                self.assertIn("open-nsfw", self.body(refused)["error"])
            run.assert_not_called()

            unknown = self.post("/api/open-card", json.dumps({"card": "free-parking"}), cookie=cookie)
            self.assertEqual(unknown.status, 404)
            run.assert_not_called()

            outside = self.post("/api/open-path", json.dumps({"path": "/etc/passwd"}), cookie=cookie)
            self.assertEqual(outside.status, 403)
            dotted = self.post("/api/open-path", json.dumps({"path": "../org/secret"}), cookie=cookie)
            self.assertEqual(dotted.status, 403)
            run.assert_not_called()

    def test_open_card_only_launches_the_resolved_directory(self):
        target = self.board / "Pacific" / "MIDI"
        with patch("hub_server.subprocess.run") as run:
            opened = self.post(
                "/api/open-card",
                json.dumps({"card": "pacific", "sub": "midi"}),
                cookie=self.auth_cookie(),
            )
        self.assertEqual(opened.status, 200)
        self.assertEqual(self.body(opened)["opened"], [str(target)])
        self.assertTrue(target.is_dir())
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["open", str(target)])

    def test_sparsebundle_password_stays_on_stdin(self):
        with patch("hub_server.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stderr=b"")
            missing = hub_server.ensure_fun_mounted("mount-token")
            self.assertEqual(missing, ["sparsebundle-missing"])
            run.assert_not_called()

            mounted = Path(self.tmp.name) / "mounted"
            mounted.mkdir()
            hub_server.FUN_VOL_CANDIDATES = [mounted]
            already = hub_server.ensure_fun_mounted("mount-token")
            self.assertEqual(already, ["already-mounted"])
            run.assert_not_called()

            bundle = Path(self.tmp.name) / "Fun.sparsebundle"
            bundle.mkdir()
            (bundle / "Info.bckup").write_bytes(b"bplist-test")
            hub_server.FUN_VOL_CANDIDATES = [Path(self.tmp.name) / "absent"]
            hub_server.SPARSE = bundle
            notes = hub_server.ensure_fun_mounted("mount-token")
        self.assertEqual((bundle / "Info.plist").read_bytes(), b"bplist-test")
        self.assertIn("restored-Info.plist", notes)
        self.assertIn("hdiutil-rc=0", notes)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["hdiutil", "attach", "-stdinpass"])
        self.assertNotIn("mount-token", argv)
        self.assertEqual(run.call_args.kwargs["input"], b"mount-token")


if __name__ == "__main__":
    unittest.main()
