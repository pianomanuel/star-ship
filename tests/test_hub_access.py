"""Regression tests for hub login, session storage, and path allow-listing."""
import hashlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "hub" / "server"
sys.path.insert(0, str(SERVER))

import hub_extras  # noqa: E402
import hub_server  # noqa: E402


class _Req:
    def __init__(self, headers, https=False):
        self.headers = headers
        self._https = https

    def is_https(self):
        return self._https


class HubAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.org = base / "org"
        self.board = base / "board"
        self.onedrive = base / "onedrive"
        self.root = base / "hubroot"
        self.maindb = self.home / "Documents" / "MainDB"
        self.sec = base / "secure" / "security.db"
        self.sessions = base / "secure" / "sessions.db"
        for path in (self.org, self.board, self.onedrive, self.root, self.maindb, self.sec.parent):
            path.mkdir(parents=True, exist_ok=True)

        self._prev = {
            "HOME": hub_server.HOME,
            "ORG": hub_server.ORG,
            "BOARD": hub_server.BOARD,
            "ONEDRIVE_DOCS": hub_server.ONEDRIVE_DOCS,
            "ROOT": hub_server.ROOT,
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
        hub_server.ONEDRIVE_DOCS = self.onedrive
        hub_server.ROOT = self.root
        hub_server.SEC_DB = self.sec
        hub_server.MAIN_SEC = base / "missing-main-security.db"
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
        hub_server.ONEDRIVE_DOCS = self._prev["ONEDRIVE_DOCS"]
        hub_server.ROOT = self._prev["ROOT"]
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

    def test_account_create_stores_salted_hash_and_refuses_a_second_account(self):
        short = hub_extras.create_hub_account("Robert", "short", "hint")
        self.assertFalse(short["ok"])
        self.assertFalse(hub_extras.hub_account_exists())
        self.assertFalse(self.sec.exists())

        created = hub_extras.create_hub_account("   ", "bookmark-ok", "  " + ("h" * 100))
        self.assertTrue(created["ok"])
        self.assertEqual(created["user"], "Robert")
        self.assertEqual(created["hint"], "h" * 80)
        self.assertTrue(hub_extras.hub_account_exists())

        con = sqlite3.connect(self.sec)
        secrets = dict(con.execute("SELECT key, value FROM secrets"))
        con.close()
        self.assertNotIn("bookmark-ok", secrets.values())
        self.assertEqual(len(secrets["hub_login_salt"]), 32)
        self.assertEqual(len(secrets["hub_login_hash"]), 64)
        self.assertEqual(secrets["hub_login_user"], "Robert")
        self.assertTrue(hub_server.check_hub_password("bookmark-ok"))
        self.assertFalse(hub_server.check_hub_password("Bookmark-ok"))
        self.assertFalse(hub_server.check_hub_password(""))

        again = hub_extras.create_hub_account("Other", "bookmark-ok-2")
        self.assertFalse(again["ok"])
        self.assertIn("already exists", again["error"])

    def test_password_check_fails_closed_without_an_account(self):
        self.assertFalse(hub_server.check_hub_password("bookmark-ok"))
        self.assertFalse(hub_server.check_hub_password(""))

    def test_session_token_is_hashed_at_rest_and_expiry_drops_it(self):
        tok = hub_server.new_session()
        self.assertTrue(hub_server.valid_session(tok))
        self.assertFalse(hub_server.valid_session(None))
        self.assertFalse(hub_server.valid_session(""))

        con = sqlite3.connect(self.sessions)
        stored = con.execute("SELECT token_sha256, expires FROM sessions").fetchone()
        con.close()
        digest = hashlib.sha256(tok.encode("utf-8")).hexdigest()
        self.assertEqual(stored[0], digest)
        self.assertNotIn(tok, stored[0])
        self.assertGreater(stored[1], time.time())

        hub_server.SESSIONS.clear()
        self.assertTrue(hub_server.valid_session(tok))

        hub_server.SESSIONS[tok] = time.time() - 1
        self.assertFalse(hub_server.valid_session(tok))
        con = sqlite3.connect(self.sessions)
        left = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        con.close()
        self.assertEqual(left, 0)
        self.assertFalse(hub_server.valid_session(tok))

    def test_open_path_stays_inside_allowed_roots(self):
        inside = self.org / "notes" / "a.txt"
        inside.parent.mkdir()
        inside.write_text("ok", encoding="utf-8")
        self.assertEqual(hub_server.resolve_open_path(str(inside)), inside.resolve())

        board_file = self.board / "card.txt"
        board_file.write_text("card", encoding="utf-8")
        self.assertEqual(hub_server.resolve_open_path("card.txt"), board_file.resolve())

        main_file = self.maindb / "index.db"
        main_file.write_text("db", encoding="utf-8")
        self.assertEqual(hub_server.resolve_open_path(str(main_file)), main_file.resolve())

        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("no", encoding="utf-8")
        self.assertIsNone(hub_server.resolve_open_path(str(outside)))
        self.assertIsNone(hub_server.resolve_open_path(""))
        self.assertIsNone(hub_server.resolve_open_path("../org/notes/a.txt"))
        self.assertIsNone(hub_server.resolve_open_path(str(self.org / ".." / "outside.txt")))

    def test_public_routes_and_blocked_files(self):
        public = hub_server.HubHandler.is_public
        blocked = hub_server.HubHandler.blocked_file
        self.assertTrue(public(None, "/login"))
        self.assertTrue(public(None, "/api/account-status"))
        self.assertTrue(public(None, "/css/hub.css"))
        self.assertFalse(public(None, "/api/file"))
        self.assertFalse(public(None, "/api/links"))
        self.assertFalse(public(None, "/CSS/hub.css"))

        self.assertTrue(blocked(None, "/hub/server/hub_server.py"))
        self.assertTrue(blocked(None, "/secure/security.db"))
        self.assertTrue(blocked(None, "/certs/main_thing.PEM"))
        self.assertTrue(blocked(None, "/.env"))
        self.assertFalse(blocked(None, "/css/hub.css"))
        self.assertFalse(blocked(None, "/"))

    def test_html_redirect_only_for_public_site_pages(self):
        redirect = hub_server.HubHandler.wants_html_redirect
        host = {"Host": "pianomanuel.com:8444", "Accept": "text/html"}
        self.assertTrue(redirect(_Req(host), "/login"))
        self.assertTrue(redirect(_Req(host), "/"))
        self.assertFalse(redirect(_Req(host, https=True), "/login"))
        self.assertFalse(redirect(_Req(host), "/api/file"))
        self.assertFalse(redirect(_Req(host), "/css/hub.css"))
        self.assertFalse(redirect(_Req({"Host": "127.0.0.1:8080", "Accept": "text/html"}), "/login"))
        self.assertTrue(
            redirect(
                _Req({"Host": "www.pianomanuel.com", "Accept": "text/html,application/xhtml+xml"}),
                "/modules/ucc",
            )
        )


if __name__ == "__main__":
    unittest.main()
