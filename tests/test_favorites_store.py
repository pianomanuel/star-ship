"""Regression tests for bookmark URL identity, validation, and archive rules."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hub" / "server"))

import favorites_store  # noqa: E402


class FavoritesStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "favorites.db"
        self._prev = favorites_store.DB_PATH
        favorites_store.DB_PATH = self.db_path
        con = sqlite3.connect(self.db_path)
        con.executescript(favorites_store.SCHEMA)
        con.close()

    def tearDown(self):
        favorites_store.DB_PATH = self._prev
        self.tmp.cleanup()

    def test_norm_url_collapses_identity_and_rejects_unusable(self):
        same = "http://example.com/a/b?B=2&a=1"
        self.assertEqual(
            favorites_store.norm_url(
                "  HTTPS://WWW.Example.com:443/a/b/?B=2&utm_source=x&a=1#frag  "
            ),
            same,
        )
        self.assertEqual(
            favorites_store.norm_url("http://user:pw@example.com/a/b?fbclid=1&a=1&B=2"),
            same,
        )
        self.assertEqual(favorites_store.norm_url("http://example.com"), "http://example.com/")
        self.assertEqual(
            favorites_store.norm_url("http://example.com:8080/x"),
            "http://example.com:8080/x",
        )
        self.assertEqual(
            favorites_store.norm_url("http://example.com/q?utm=keep&Utm_source=drop&ok=2"),
            "http://example.com/q?ok=2&utm=keep",
        )
        self.assertEqual(favorites_store.norm_url("mailto:User@Example.com"), "mailto:User@Example.com")
        for bad in ("", "   ", None, "javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,hi", "about:blank", "chrome://flags", "place:sort=8"):
            self.assertIsNone(favorites_store.norm_url(bad), bad)

    def test_add_rejects_invalid_and_dedupes_normalized_urls(self):
        first = favorites_store.add_link({
            "url": "https://www.Example.com/a/?utm_medium=email",
            "title": "Example",
            "favorite": True,
            "tags": "Art, art, , BETA",
            "folder": "Reading",
        })
        self.assertTrue(first["ok"])
        again = favorites_store.add_link({"url": "http://example.com/a"})
        self.assertEqual(again["error"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertFalse(again["archived"])
        bad = favorites_store.add_link({"url": "javascript:alert(1)"})
        self.assertEqual(bad["error"], "invalid url")

        listed = favorites_store.list_links()
        self.assertTrue(listed["ok"])
        self.assertEqual(listed["total"], 1)
        item = listed["items"][0]
        self.assertEqual(item["title"], "Example")
        self.assertEqual(item["favorite"], 1)
        self.assertEqual(item["tags"], ["art", "beta"])
        self.assertNotIn("url_norm", item)
        self.assertEqual([f["name"] for f in listed["folders"]], ["Reading"])

    def test_tag_filter_is_exact_and_inputs_are_bounded(self):
        favorites_store.add_link({"url": "https://art.example/a", "tags": ["art"]})
        favorites_store.add_link({"url": "https://cartoon.example/b", "tags": ["cartoon"]})
        long_tags = [f"t{i}" for i in range(21)]
        long_tags[0] = "A" * 41
        added = favorites_store.add_link({"url": "https://tags.example/c", "tags": long_tags, "title": "T" * 600})
        self.assertTrue(added["ok"])

        art = favorites_store.list_links(tag="art")
        self.assertEqual([i["url"] for i in art["items"]], ["https://art.example/a"])
        self.assertEqual(favorites_store.list_links(tag="car")["items"], [])

        stored = favorites_store.list_links(q="tags.example")["items"][0]
        self.assertEqual(len(stored["title"]), 500)
        self.assertEqual(len(stored["tags"]), 20)
        self.assertEqual(stored["tags"][0], "a" * 40)

        self.assertEqual(len(favorites_store.list_links(limit=0)["items"]), 1)
        self.assertEqual(favorites_store.list_links(limit=5000)["total"], 3)
        self.assertEqual(len(favorites_store.list_links(offset=-4)["items"]), 3)
        quoted = favorites_store.list_links(q="O'Reilly")
        self.assertEqual(quoted["items"], [])

    def test_edit_blocks_invalid_and_duplicate_targets(self):
        a = favorites_store.add_link({"url": "https://a.example/1", "title": "A", "notes": "keep"})
        b = favorites_store.add_link({"url": "https://b.example/2", "title": "B"})
        missing = favorites_store.edit_link({"id": 0, "title": "nope"})
        self.assertEqual(missing["error"], "not found")
        invalid = favorites_store.edit_link({"id": b["id"], "url": "data:text/plain,x"})
        self.assertEqual(invalid["error"], "invalid url")
        dup = favorites_store.edit_link({"id": b["id"], "url": "http://a.example/1"})
        self.assertEqual(dup["error"], "duplicate")

        edited = favorites_store.edit_link({"id": b["id"], "title": "Bee", "tags": ["one"]})
        self.assertTrue(edited["ok"])
        replaced = favorites_store.edit_link({"id": b["id"], "tags": ["two"]})
        self.assertTrue(replaced["ok"])
        rows = {i["id"]: i for i in favorites_store.list_links()["items"]}
        self.assertEqual(rows[a["id"]]["url"], "https://a.example/1")
        self.assertEqual(rows[a["id"]]["notes"], "keep")
        self.assertEqual(rows[b["id"]]["title"], "Bee")
        self.assertEqual(rows[b["id"]]["url"], "https://b.example/2")
        self.assertEqual(rows[b["id"]]["tags"], ["two"])

    def test_archive_is_soft_and_protected_links_cannot_be_archived(self):
        plain = favorites_store.add_link({"url": "https://plain.example/"})
        with favorites_store.connect() as db:
            db.execute(
                "INSERT INTO links(url, url_norm, title, favorite, protected, archived, status, source) "
                "VALUES(?, ?, 'Locked', 0, 1, 0, 'unchecked', 'test')",
                ("https://locked.example/x", favorites_store.norm_url("https://locked.example/x")),
            )
            locked_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        refused = favorites_store.archive_link({"id": locked_id})
        self.assertEqual(refused["error"], "protected link: archive refused")
        active = favorites_store.list_links()
        self.assertEqual(active["counts"]["protected"], 1)
        self.assertIn(locked_id, [i["id"] for i in active["items"]])

        archived = favorites_store.archive_link({"id": plain["id"]})
        self.assertTrue(archived["ok"])
        self.assertTrue(archived["archived"])
        self.assertEqual(favorites_store.list_links()["total"], 1)
        only_archived = favorites_store.list_links(archived=True)
        self.assertEqual([i["id"] for i in only_archived["items"]], [plain["id"]])

        again = favorites_store.add_link({"url": "http://plain.example"})
        self.assertEqual(again["error"], "duplicate")
        self.assertTrue(again["archived"])

        restored = favorites_store.archive_link({"id": plain["id"]}, restore=True)
        self.assertTrue(restored["ok"])
        self.assertFalse(restored["archived"])
        self.assertEqual(favorites_store.list_links(archived=True)["total"], 0)

        with favorites_store.connect() as db:
            still = db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            locked_flag = db.execute("SELECT archived FROM links WHERE id=?", (locked_id,)).fetchone()[0]
        self.assertEqual(still, 2)
        self.assertEqual(locked_flag, 0)
        self.assertEqual(favorites_store.archive_link({"id": 999})["error"], "not found")

    def test_toggle_favorite_flips_existing_rows_only(self):
        added = favorites_store.add_link({"url": "https://fav.example/", "favorite": False})
        on = favorites_store.toggle_favorite({"id": added["id"]})
        off = favorites_store.toggle_favorite({"id": added["id"]})
        self.assertTrue(on["favorite"])
        self.assertFalse(off["favorite"])
        self.assertEqual(favorites_store.toggle_favorite({"id": 404})["error"], "not found")

    def test_list_reports_missing_database(self):
        favorites_store.DB_PATH = Path(self.tmp.name) / "missing.db"
        result = favorites_store.list_links()
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])


if __name__ == "__main__":
    unittest.main()
