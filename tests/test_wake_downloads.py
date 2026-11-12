"""Regression tests for verified downloads and the wake-report PDF."""
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "hub" / "server"
sys.path.insert(0, str(SERVER))

import hub_extras  # noqa: E402


def _pdf_stream(data: bytes) -> bytes:
    start = data.index(b"stream\n") + len(b"stream\n")
    end = data.index(b"\nendstream", start)
    return data[start:end]


class WakeDownloadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.org = Path(self.tmp.name) / "org"
        self.home.mkdir()
        self.org.mkdir()
        self._prev = (hub_extras.HOME, hub_extras.ORG)
        hub_extras.HOME = self.home
        hub_extras.ORG = self.org

    def tearDown(self):
        hub_extras.HOME, hub_extras.ORG = self._prev
        self.tmp.cleanup()

    def _touch(self, path: Path, payload: bytes = b"ok") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def test_verified_downloads_keep_kind_and_skip_empty_files(self):
        desk = self.home / "Desktop" / "Gloria-Peters-2026"
        gloria = self.org / "Gloria" / "ingest-2026-09-18"
        reports = self.org / "Reports-Excel"
        score = desk / "Gloria-SATB-Organ-JRM-IV-2026.mxl"
        empty_pack = desk / "Gloria-pack-2026-09-18.zip"
        full = gloria / "Gloria-2026-09-05-Full-score.mxl"
        tags = reports / "Tags-Sort-2026-09-18.md"
        self._touch(score, b"score")
        self._touch(empty_pack, b"")
        self._touch(full, b"full-score")
        self._touch(tags, b"tags")

        gloria_items = hub_extras.verified_download_items("gloria")
        self.assertEqual([item["filename"] for item in gloria_items], [score.name, full.name])
        self.assertTrue(all(item["url"].startswith("/api/file?path=") for item in gloria_items))
        self.assertEqual(urllib.parse.unquote(gloria_items[0]["url"].split("path=", 1)[1]), str(score))
        self.assertIn(f"{score.stat().st_size} bytes", gloria_items[0]["note"])

        tag_items = hub_extras.verified_download_items("tags")
        self.assertEqual([item["filename"] for item in tag_items], [tags.name])

        downloads = {item["filename"] for item in hub_extras.verified_download_items("downloads")}
        self.assertEqual(downloads, {score.name, full.name, tags.name})
        self.assertEqual(hub_extras.verified_download_items("Downloads"), [])
        self.assertEqual(hub_extras.verified_download_items("missing"), [])

    def test_wake_pdf_escapes_text_and_caps_the_page(self):
        long_line = "(" * 100 + "\\" + ")"
        summary = "\n".join([long_line, "snow\u2603man", "line two"] + [f"row {i}" for i in range(50)])
        out = hub_extras.write_wake_pdf(summary)

        self.assertEqual(out, self.org / "Reports-Excel" / "Wake-Report-latest.pdf")
        data = out.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertIn(b"%%EOF", data)
        stream = _pdf_stream(data)
        length = int(data.split(b"/Length ", 1)[1].split(b" >>", 1)[0])
        self.assertEqual(length, len(stream))

        text = stream.decode("latin-1")
        escaped = ("(" * 88).replace("(", "\\(")
        self.assertIn(f"({escaped}) Tj", text)
        self.assertNotIn("(" * 89, text)
        self.assertIn("(snow?man) Tj", text)
        self.assertIn("(line two) Tj", text)
        self.assertIn("(row 38) Tj", text)
        self.assertNotIn("(row 39) Tj", text)
        self.assertEqual(text.count(") Tj"), 44)

        blank = hub_extras.write_wake_pdf("")
        blank_text = _pdf_stream(blank.read_bytes()).decode("latin-1")
        self.assertIn(r"(\(empty\)) Tj", blank_text)

        spaced = _pdf_stream(hub_extras.write_wake_pdf("\n").read_bytes()).decode("latin-1")
        self.assertNotIn(r"(\(empty\)) Tj", spaced)
        self.assertIn("() Tj", spaced)

    def test_wake_report_stays_inside_the_org_and_marks_databases(self):
        mdb = self.home / "Documents" / "MainDB"
        self._touch(mdb / "tags_main.db", b"db")
        self._touch(mdb / "main_thing.db", b"")
        locked = self.org / "Gloria" / "Score-MASTER-LOCKED.mxl"
        self._touch(locked, b"locked")

        payload = hub_extras.wake_report_payload()
        pdf = Path(payload["pdf_path"])
        self.assertTrue(payload["ok"])
        self.assertTrue(pdf.is_file())
        self.assertEqual(pdf, self.org / "Reports-Excel" / "Wake-Report-latest.pdf")
        self.assertTrue(str(pdf).startswith(str(self.org)))
        self.assertEqual(urllib.parse.unquote(payload["pdf_url"].split("path=", 1)[1]), str(pdf))

        summary = payload["summary"]
        self.assertTrue(summary.startswith("Wake report — "))
        self.assertIn("Disk free:", summary)
        self.assertIn("tags_main.db: OK", summary)
        self.assertIn("main_thing.db: MISSING", summary)
        self.assertIn("index_mirror.db: MISSING", summary)
        self.assertIn("Gloria MASTER LOCKED: YES", summary)
        self.assertIn("verified downloads only", summary)

        locked.unlink()
        absent = hub_extras.wake_report_payload()
        self.assertIn("Gloria MASTER LOCKED: NO", absent["summary"])


if __name__ == "__main__":
    unittest.main()
