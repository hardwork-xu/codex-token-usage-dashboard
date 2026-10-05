"""Synthetic descendant metadata recovery; no account, real logs or Codex calls."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import descendants


class DescendantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.root = self.log("root")
        self.records = {"root": {"path": self.root}}

    def log(self, ident, parent=None, **extra):
        source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "vscode"
        value = {"id": ident, "source": source, "privatePrompt": "private-header-marker", **extra}
        target = self.folder / (ident + ".jsonl")
        target.write_text(json.dumps({"type": "session_meta", "payload": value}) + "\n", encoding="utf-8")
        return str(target)

    def row(self, ident, parent="root", **extra):
        return {"id": ident, "parentThreadId": parent, "path": self.log(ident, parent),
                "preview": "private-preview-marker", "turns": [{"text": "private-message-marker"}], **extra}

    def page(self, rows, following=None, request=2):
        return {"id": request, "result": {"data": rows, "nextCursor": following}}

    def run_discovery(self, pages, *, records=None, failure=None, **options):
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        def responses():
            yield {"id": 1, "result": {}}
            yield from pages
            if failure:
                raise failure
        rpc.responses.side_effect = responses
        with mock.patch.object(descendants, "JsonRpcProcess", return_value=rpc) as transport:
            result = descendants.discover_descendants(records or self.records, "root", ["synthetic-codex"], **options)
        sent = [call.args[0] for call in rpc.send.call_args_list]
        return result, sent, transport

    def test_public_ancestor_filter_and_headers_allow_only_descendant_metadata(self):
        child = self.row("child")
        result, sent, transport = self.run_discovery([self.page([child])])
        self.assertEqual(result["threads"], [{"threadId": "child", "parentThreadId": "root", "path": child["path"]}])
        self.assertTrue(result["complete"])
        self.assertEqual((result["pages"], result["checked"]), (1, 1))
        self.assertEqual([row["method"] for row in sent], ["initialize", "initialized", "thread/list"])
        params = sent[-1]["params"]
        self.assertEqual(params["ancestorThreadId"], "root")
        self.assertTrue(params["useStateDbOnly"])
        self.assertEqual((params["sortKey"], params["sortDirection"]), ("created_at", "asc"))
        self.assertTrue(sent[0]["params"]["capabilities"]["experimentalApi"])
        self.assertNotIn("private-", json.dumps(result))
        self.assertEqual(transport.call_args.args[0], ["synthetic-codex", "app-server", "--stdio"])

    def test_full_chain_is_verified_independently_of_response_order(self):
        child, grandchild = self.row("child"), self.row("grandchild", "child")
        result, _, _ = self.run_discovery([self.page([grandchild, child])])
        self.assertEqual({r["threadId"] for r in result["threads"]}, {"child", "grandchild"})

    def test_later_page_uses_exact_registered_ancestor_header(self):
        child, grandchild = self.row("child"), self.row("grandchild", "child")
        first, sent, _ = self.run_discovery([self.page([child], "next")], max_pages=1)
        self.assertFalse(first["complete"])
        self.assertEqual(first["nextCursor"], "next")
        records = self.records | {"child": {"path": child["path"]}}
        second, sent, _ = self.run_discovery([self.page([grandchild])], records=records, cursor="next")
        self.assertEqual(sent[-1]["params"]["cursor"], "next")
        self.assertEqual(second["threads"][0]["threadId"], "grandchild")

    def test_missing_ancestor_is_deferred_without_guessing_from_leaf_source(self):
        result, _, _ = self.run_discovery([self.page([self.row("grandchild", "unseen-parent")])])
        self.assertEqual(result["threads"], [])
        self.assertEqual(result["deferred"], 1)

    def test_registered_other_root_cannot_admit_its_descendant(self):
        row = self.row("foreign-child", "other-root")
        records = self.records | {"other-root": {"path": self.log("other-root")}}
        result, _, _ = self.run_discovery([self.page([row])], records=records)
        self.assertEqual(result["threads"], [])
        self.assertEqual(result["rejected"], 1)

    def test_mismatched_header_id_parent_and_top_level_parent_are_rejected(self):
        for bad_kind in ("id", "source-parent", "top-parent"):
            with self.subTest(bad_kind=bad_kind):
                row = self.row("child")
                if bad_kind == "id":
                    row["path"] = self.log("foreign")
                elif bad_kind == "source-parent":
                    self.log("child", "other-parent")
                else:
                    self.log("child", "root", parent_thread_id="other-parent")
                result, _, _ = self.run_discovery([self.page([row])])
                self.assertEqual(result["threads"], [])
                self.assertEqual(result["rejected"], 1)

    def test_cycle_or_conflicting_duplicate_cannot_reach_root(self):
        a, b = self.row("a", "b"), self.row("b", "a")
        result, _, _ = self.run_discovery([self.page([a, b])])
        self.assertEqual(result["threads"], [])
        self.assertEqual(result["rejected"], 2)
        child, grandchild = self.row("child"), self.row("grandchild", "child")
        bad = child | {"parentThreadId": "other-root"}
        result, _, _ = self.run_discovery([self.page([child, bad, grandchild])])
        self.assertEqual(result["threads"], [])
        self.assertEqual(result["rejected"], 2)

    def test_symlink_leaf_is_rejected(self):
        child = self.row("child")
        link = self.folder / "alias.jsonl"
        try:
            link.symlink_to(child["path"])
        except OSError:
            self.skipTest("Symlink creation unavailable on this platform")
        result, _, _ = self.run_discovery([self.page([child | {"path": str(link)}])])
        self.assertEqual(result["threads"], [])

    def test_truncated_oversized_and_nonobject_headers_are_rejected(self):
        for content in ('[]\n', '{"type":"session_meta"}', 'x' * 2049 + '\n'):
            with self.subTest(content=content[:30]):
                row = self.row("child")
                Path(row["path"]).write_text(content, encoding="utf-8")
                with mock.patch.object(descendants, "MAX_HEADER_BYTES", 2048):
                    result, _, _ = self.run_discovery([self.page([row])])
                self.assertEqual(result["threads"], [])

    def test_bounded_pages_preserve_resume_cursor_and_archived_scope(self):
        result, sent, _ = self.run_discovery([self.page([self.row("a")], "cursor-2"),
            self.page([self.row("b")], "cursor-3", request=3)], max_pages=2, archived=True)
        self.assertEqual(result["nextCursor"], "cursor-3")
        self.assertFalse(result["complete"])
        self.assertEqual(result["pages"], 2)
        self.assertTrue(all(r["params"]["archived"] for r in sent if r["method"] == "thread/list"))

    def test_failed_page_retains_prior_results_and_retries_failed_cursor(self):
        result, _, _ = self.run_discovery([self.page([self.row("a")], "next"),
            {"id": 3, "error": {"message": "private-server-marker"}}])
        self.assertEqual(result["threads"][0]["threadId"], "a")
        self.assertEqual(result["nextCursor"], "next")
        self.assertIsNotNone(result["error"])
        self.assertNotIn("private-server-marker", json.dumps(result))
        result, _, _ = self.run_discovery([], cursor="resume", failure=TimeoutError("private-error"))
        self.assertEqual(result["nextCursor"], "resume")

    def test_repeated_cursor_and_oversized_page_fail_before_admitting_rows(self):
        rows = [self.row("a"), self.row("b")]
        for page, options in ((self.page(rows, "resume"), {"cursor": "resume"}),
                              (self.page(rows), {"page_size": 1})):
            result, _, _ = self.run_discovery([page], **options)
            self.assertEqual(result["threads"], [])
            self.assertIsNotNone(result["error"])

    def test_unregistered_or_unverified_root_never_queries_codex(self):
        with mock.patch.object(descendants, "JsonRpcProcess") as transport:
            with self.assertRaises(ValueError):
                descendants.discover_descendants({}, "root", "synthetic-codex")
            Path(self.root).write_text('[]\n', encoding="utf-8")
            result = descendants.discover_descendants(self.records, "root", "synthetic-codex")
            self.assertIsNotNone(result["error"])
            transport.assert_not_called()

    def test_budget_and_cursor_inputs_are_bounded(self):
        for options in ({"max_pages": 0}, {"max_pages": True}, {"max_pages": 6},
                        {"page_size": 101}, {"timeout": float("nan")}, {"timeout": 31},
                        {"cursor": "x" * 4097}, {"archived": 1}):
            with self.subTest(options=options), mock.patch.object(descendants, "JsonRpcProcess") as transport:
                with self.assertRaises(ValueError):
                    descendants.discover_descendants(self.records, "root", "synthetic-codex", **options)
                transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
