"""Regression tests for Netscape bookmark seeding and the overwrite guard."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "hub" / "tools" / "build_favorites_db.py"

BOOKMARKS = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
<DT><H3>★ ManuelOps (Protected)</H3>
<DL><p>
<DT><A HREF="https://www.safe.example/a/?utm_source=x" ADD_DATE="1700000000">Safe</A>
<DT><H3>Nested</H3>
<DL><p>
<DT><A HREF="https://nested.example/b">Nested link</A>
<DT><A HREF="javascript:alert(1)">Bad</A>
</DL><p>
</DL><p>
<DT><H3>Favorites (cleaned)</H3>
<DL><p>
<DT><A HREF="http://fav.example/c">Fav</A>
</DL><p>
</DL>
"""


class FavoritesSeedTest(unittest.TestCase):
    def test_protected_flag_is_direct_folder_only_and_scripts_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = root / "bookmarks.html"
            html.write_text(BOOKMARKS, encoding="utf-8")
            out = root / "favorites.db"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(out), "--netscape", str(html)],
                check=True, capture_output=True, text=True,
            )
            stats = json.loads(proc.stdout)
            self.assertEqual(stats["skipped_invalid"], 1)
            self.assertEqual(stats["inserted"], 3)

            con = sqlite3.connect(out)
            rows = con.execute(
                "SELECT url, protected, favorite, folder, tags FROM links_v ORDER BY url"
            ).fetchall()
            con.close()
            by_url = {row[0]: row for row in rows}
            safe = by_url["https://www.safe.example/a/?utm_source=x"]
            nested = by_url["https://nested.example/b"]
            fav = by_url["http://fav.example/c"]
            self.assertEqual(safe[1:], (1, 1, "★ ManuelOps (Protected)", "protected"))
            self.assertEqual(nested[1:], (0, 0, "Nested", None))
            self.assertEqual(fav[1], 0)
            self.assertEqual(fav[2], 1)
            self.assertIn("Favorites", fav[3])
            self.assertEqual(fav[4], "favorites-cleaned")
            self.assertNotIn("javascript:alert(1)", by_url)

    def test_existing_database_is_not_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html = root / "bookmarks.html"
            html.write_text(BOOKMARKS, encoding="utf-8")
            out = root / "favorites.db"
            subprocess.run(
                [sys.executable, str(SCRIPT), str(out), "--netscape", str(html)],
                check=True, capture_output=True, text=True,
            )
            before = out.read_bytes()
            again = subprocess.run(
                [sys.executable, str(SCRIPT), str(out), "--netscape", str(html)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("refusing", again.stderr + again.stdout)
            self.assertEqual(out.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
