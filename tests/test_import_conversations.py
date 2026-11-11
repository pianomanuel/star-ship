"""Regression tests for conversation export parsing and idempotent import."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "import-pipeline"))

import import_conversations as ic  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_timestamps_accept_vendor_shapes(self):
        self.assertEqual(ic.ts_to_iso("2024-01-02T03:04:05Z"), "2024-01-02T03:04:05Z")
        self.assertTrue(ic.ts_to_iso(1_700_000_000).startswith("2023-11-14T22:13:20"))
        self.assertEqual(
            ic.ts_to_iso(1_700_000_000),
            ic.ts_to_iso({"$date": {"$numberLong": "1700000000000"}}),
        )
        self.assertEqual(ic.ts_to_iso(1_700_000_000), ic.ts_to_iso({"$date": 1_700_000_000_000}))
        self.assertIsNone(ic.ts_to_iso(None))
        self.assertIsNone(ic.ts_to_iso({"$date": {"$numberLong": "not-a-number"}}))

    def test_sender_and_format_sniff(self):
        self.assertEqual(ic.norm_sender("End-User"), "human")
        self.assertEqual(ic.norm_sender("model"), "assistant")
        self.assertEqual(ic.sniff_format({"conversations": []}, "prod-grok-backend.json"), "grok")
        self.assertEqual(
            ic.sniff_format([{"id": "1", "mapping": {"n": {}}}], "export.json"),
            "chatgpt",
        )
        self.assertEqual(
            ic.sniff_format([{"uuid": "u", "name": "N", "chat_messages": []}], "c.json"),
            "claude",
        )
        self.assertEqual(
            ic.sniff_format({"chats": [{"messages": [{"role": "model", "content": "hi"}]}]}, "x.json"),
            "gemini",
        )
        self.assertEqual(ic.sniff_format({"sessions": []}, "chat.json"), "deepseek")
        self.assertEqual(ic.sniff_format(None, None), "generic")

    def test_vendor_parsers_keep_order_and_drop_empty_or_idless_rows(self):
        grok = ic.parse_grok_conversations([
            {"conversation": {"title": "skip"}},
            {
                "conversation": {
                    "id": "g1",
                    "title": "Grok",
                    "create_time": {"$date": {"$numberLong": "1700000000000"}},
                },
                "responses": [
                    {"response": {"sender": "Human", "message": "hi", "create_time": 1_700_000_000}},
                    {"response": {"sender": "assistant", "message": "yo", "model": "grok"}},
                ],
            },
        ])
        self.assertEqual(len(grok), 1)
        self.assertEqual(grok[0][0], "g1")
        self.assertEqual([turn[0] for turn in grok[0][3]], ["human", "assistant"])
        self.assertEqual(grok[0][3][1][3], "grok")

        chatgpt = ic.parse_chatgpt([{
            "id": "c1",
            "title": "Hello",
            "create_time": 1_700_000_000,
            "mapping": {
                "late": {"message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["second"], "model": "gpt"},
                    "create_time": 1_700_000_002,
                }},
                "tool": {"message": {
                    "author": {"role": "tool"},
                    "content": {"parts": ["hidden"]},
                    "create_time": 1_700_000_001.5,
                }},
                "early": {"message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["first", {"x": 1}]},
                    "create_time": 1_700_000_001,
                }},
                "blank": {"message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["   "]},
                    "create_time": 1_700_000_003,
                }},
            },
        }])
        self.assertEqual([(t[0], t[1]) for t in chatgpt[0][3]], [("human", "first"), ("assistant", "second")])

        claude = ic.parse_claude([{
            "uuid": "u1",
            "name": "Named",
            "chat_messages": [
                {"sender": "human", "text": [{"text": "a"}, {"text": "b"}]},
                {"sender": "assistant", "text": "ok", "model": "claude"},
                "skip",
            ],
        }])
        self.assertEqual(claude[0][1], "Named")
        self.assertEqual([(t[0], t[1]) for t in claude[0][3]], [("human", "a\nb"), ("assistant", "ok")])
        self.assertEqual(ic.parse_claude([{"name": "no-id"}]), [])


class ImporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = (ic.DB, ic.REPORTS, ic.INBOX)
        ic.DB = root / "conversations.sqlite3"
        ic.REPORTS = root / "reports"
        ic.INBOX = root / "missing-inbox"
        self.imp = ic.Importer()

    def tearDown(self):
        ic.DB, ic.REPORTS, ic.INBOX = self._prev
        self.tmp.cleanup()

    def test_import_is_idempotent_and_skips_blank_messages(self):
        path = Path(self.tmp.name) / "conversations.json"
        path.write_text(json.dumps([{
            "id": "c1",
            "title": "Hello",
            "create_time": 1_700_000_000,
            "mapping": {
                "a": {"message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["first"]},
                    "create_time": 1,
                }},
                "b": {"message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": [""]},
                    "create_time": 2,
                }},
            },
        }]), encoding="utf-8")

        self.assertIn("[chatgpt] ok, 1 new conversations", self.imp.import_json(path))
        self.assertIn("already imported", self.imp.import_json(path))
        self.assertEqual(self.imp.stats["skipped_dup"], 1)

        con = sqlite3.connect(ic.DB)
        messages = con.execute("SELECT sender, text FROM messages ORDER BY id").fetchall()
        count = con.execute("SELECT message_count FROM conversations").fetchone()[0]
        con.close()
        self.assertEqual(messages, [("human", "first")])
        self.assertEqual(count, 1)

        self.assertFalse(self.imp.add_conversation("chatgpt", "c1", "Hello", None, [("human", "again", None, None)]))
        same_text = self.imp.add_conversation(
            "chatgpt", "c2", "Other", None,
            [("human", "first", None, None), ("human", "first", None, None), ("assistant", "", None, None)],
        )
        self.assertTrue(same_text)
        con = sqlite3.connect(ic.DB)
        second = con.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=(SELECT id FROM conversations WHERE source_id='c2')"
        ).fetchone()[0]
        con.close()
        self.assertEqual(second, 1)

    def test_markdown_and_missing_inbox(self):
        note = Path(self.tmp.name) / "note.md"
        note.write_text("pasted chat", encoding="utf-8")
        self.assertIn("[markdown] ok", self.imp.import_markdown(note))
        self.assertIn("already imported", self.imp.import_markdown(note))
        self.assertEqual(self.imp.scan(), ["inbox missing"])


if __name__ == "__main__":
    unittest.main()
