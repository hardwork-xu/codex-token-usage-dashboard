"""Durable archive tests use synthetic numeric metadata only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usage_archive


def tokens(input_=80, output=20, cached=10, reasoning=5, write=0):
    return {"total": input_ + output, "input": input_, "cachedInput": cached,
            "cacheWriteInput": write, "output": output, "reasoningOutput": reasoning}


def fixture(thread="thread-test", amount=None, *, complete=True):
    amount = tokens() if amount is None else amount
    metadata = {"model": "gpt-6-astra", "serviceTier": "priority", "pricingMetadataStatus": "known"}
    turn = {"id": "turn-1", "threadId": thread, "startedAt": 1791820800, "endedAt": None,
            "status": "running", "quality": "complete", "note": "仅统计该任务自身，子代理另列。",
            "tokens": deepcopy(amount), "dailyUsage": {"2026-10-13": deepcopy(amount)},
            "undatedTokens": tokens(0, 0, 0, 0),
            "usageSlices": [{"day": "2026-10-13", "speedOverrideDay": None, **metadata,
                             "apiContext": "short", "tokens": deepcopy(amount)}],
            "usageSegments": [{"timestamp": 1791820800, **metadata,
                               "apiContext": "short", "tokens": deepcopy(amount)}], **metadata}
    return {"threadId": thread, "identityVerified": True,
            "conversationMetadata": {"sourceType": "main", "parentThreadId": None, "agentLabel": None},
            "turns": [turn], "warnings": [], "sourceNote": "本地记录格式可能随 Codex 更新。",
            "reading": {"complete": complete, "bytesRead": 100 if complete else 50, "fileBytes": 100}}


class UsageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.archive = usage_archive.UsageArchive(self.folder)

    def mutate_payload(self, fn, *, rehash=False):
        with closing(sqlite3.connect(self.archive.path)) as connection, connection:
            payload = connection.execute("SELECT payload FROM snapshots WHERE thread_id='thread-test'").fetchone()[0]
            value = json.loads(payload)
            fn(value)
            payload = json.dumps(value).encode()
            if rehash:
                connection.execute("UPDATE snapshots SET payload=?,content_hash=?", (payload, hashlib.sha256(payload).hexdigest()))
            else:
                connection.execute("UPDATE snapshots SET payload=?", (payload,))

    def test_lazy_creation_empty_lookup_does_not_create_database(self):
        self.assertIsNone(self.archive.load("thread-test"))
        self.assertEqual(self.archive.stats(), {"savedThreads": 0, "invalidatedThreads": 0})
        self.assertFalse(self.archive.folder.exists())

    def test_restart_retains_exact_metadata_after_original_log_disappears(self):
        log = self.folder / "synthetic.jsonl"
        log.write_text("synthetic original file")
        result = fixture()
        saved = self.archive.save("thread-test", result)
        log.unlink()
        loaded = usage_archive.UsageArchive(self.folder).load("thread-test")
        self.assertEqual(loaded["result"], result)
        self.assertEqual(loaded["savedAt"], saved["savedAt"])
        self.assertEqual(self.archive.stats(), {"savedThreads": 1, "invalidatedThreads": 0})

    def test_duplicate_is_idempotent_and_does_not_advance_saved_time(self):
        with mock.patch.object(usage_archive.time, "time", side_effect=[1000, 2000]) as clock:
            first = self.archive.save("thread-test", fixture())
            second = self.archive.save("thread-test", fixture())
        self.assertEqual(first["savedAt"], second["savedAt"])
        self.assertTrue(second["unchanged"])
        self.assertFalse(second["retained"])
        self.assertEqual(clock.call_count, 1)

    def test_message_path_price_canaries_dropped_at_every_level(self):
        result = fixture()
        result.update(prompt="private-prompt-marker", path="private-path-marker", apiUsd=999)
        result["conversationMetadata"].update(title="private-title-marker", source={"instructions": "private-instructions-marker"})
        turn = result["turns"][0]
        turn.update(messages=["private-response-marker"], estimate={"usd": 900})
        turn["tokens"]["prompt"] = "private-token-marker"
        for key in ("usageSlices", "usageSegments"):
            turn[key][0].update(message="private-segment-marker", credits=123)
            turn[key][0]["tokens"]["raw"] = "private-nested-marker"
        result["reading"]["path"] = "private-reading-marker"
        self.archive.save("thread-test", result)
        self.assertNotIn(b"private-", self.archive.path.read_bytes())
        loaded = self.archive.load("thread-test")["result"]
        self.assertEqual(loaded, fixture())

    def test_unknown_model_speed_and_zero_none_are_preserved(self):
        result = fixture(amount=tokens(0, 0, 0, 0))
        turn = result["turns"][0]
        turn["startedAt"] = 0
        for target in [turn, turn["usageSlices"][0], turn["usageSegments"][0]]:
            target.update(model=None, serviceTier=None, pricingMetadataStatus="unknown")
        turn["usageSegments"][0]["timestamp"] = None
        self.archive.save("thread-test", result)
        self.assertEqual(self.archive.load("thread-test")["result"], result)

    def test_intra_turn_model_speed_switches_and_undated_unknown_usage_survive(self):
        result = fixture(amount=tokens(240, 60, 30, 15))
        turn = result["turns"][0]
        turn.update(model=None, serviceTier=None, pricingMetadataStatus="mixed",
                    dailyUsage={"2026-10-13": tokens(160, 40, 20, 10)},
                    undatedTokens=tokens(), usageSegments=[], usageSlices=[])
        contexts = [("gpt-6-astra", "default", 1791820800),
                    ("gpt-6.1-sol", "priority", 1791820801), (None, None, None)]
        for model, tier, when in contexts:
            common = {"model": model, "serviceTier": tier,
                      "pricingMetadataStatus": "known" if model else "unknown",
                      "apiContext": "short", "tokens": tokens()}
            turn["usageSegments"].append({"timestamp": when, **deepcopy(common)})
            turn["usageSlices"].append({"day": "2026-10-13" if when else None,
                                        "speedOverrideDay": None, **deepcopy(common)})
        self.archive.save("thread-test", result)
        self.assertEqual(usage_archive.UsageArchive(self.folder).load("thread-test")["result"], result)

    def test_no_token_record_round_trips(self):
        result = fixture()
        turn = result["turns"][0]
        turn.update(tokens=None, dailyUsage={}, usageSlices=[], usageSegments=[], quality="unavailable")
        self.archive.save("thread-test", result)
        self.assertIsNone(self.archive.load("thread-test")["result"]["turns"][0]["tokens"])

    def test_smaller_incomplete_restart_prefix_never_overwrites(self):
        prior = fixture(amount=tokens(160, 40, 20, 10))
        self.archive.save("thread-test", prior)
        outcome = self.archive.save("thread-test", fixture(complete=False))
        self.assertTrue(outcome["retained"])
        self.assertEqual(outcome["reason"], "tokens_decreased")
        self.assertEqual(self.archive.load("thread-test")["result"], prior)

    def test_complete_lower_correction_is_retained_as_conflict_not_summed(self):
        prior = fixture(amount=tokens(160, 40, 20, 10))
        self.archive.save("thread-test", prior)
        self.assertEqual(self.archive.save("thread-test", fixture())["reason"], "tokens_decreased")
        self.assertEqual(self.archive.load("thread-test")["result"]["turns"][0]["tokens"]["total"], 200)

    def test_loss_of_turns_never_overwrites_even_if_remaining_total_is_greater(self):
        prior = fixture()
        second = deepcopy(prior["turns"][0])
        second["id"] = "turn-2"
        prior["turns"].append(second)
        self.archive.save("thread-test", prior)
        outcome = self.archive.save("thread-test", fixture(amount=tokens(1000, 100, 20, 10)))
        self.assertEqual(outcome["reason"], "turns_missing")
        self.assertEqual(self.archive.load("thread-test")["result"], prior)

    def test_all_six_counters_are_protected_individually(self):
        prior = fixture(amount=tokens(100, 20, 20, 10, 5))
        self.archive.save("thread-test", prior)
        current = fixture(amount=tokens(200, 40, 20, 10, 4))
        self.assertTrue(self.archive.save("thread-test", current)["retained"])

    def test_complete_metadata_correction_replaces_equal_counts_without_adding(self):
        prior = fixture()
        for row in [prior["turns"][0], *prior["turns"][0]["usageSlices"], *prior["turns"][0]["usageSegments"]]:
            row.update(model=None, serviceTier=None, pricingMetadataStatus="unknown")
        self.archive.save("thread-test", prior)
        self.assertFalse(self.archive.save("thread-test", fixture())["retained"])
        self.assertEqual(self.archive.load("thread-test")["result"], fixture())

    def test_growing_complete_scan_atomically_replaces(self):
        self.archive.save("thread-test", fixture())
        later = fixture(amount=tokens(160, 40, 20, 10))
        later["reading"].update(bytesRead=200, fileBytes=200)
        self.assertFalse(self.archive.save("thread-test", later)["retained"])
        self.assertEqual(self.archive.load("thread-test")["result"], later)

    def test_complete_snapshot_survives_equal_incomplete_restart_scan(self):
        self.archive.save("thread-test", fixture())
        result = fixture(complete=False)
        self.assertEqual(self.archive.save("thread-test", result)["reason"], "scan_incomplete")
        result["reading"]["complete"] = True
        self.assertFalse(self.archive.save("thread-test", result)["retained"])

    def test_new_usage_partial_scan_is_durable_when_original_disappears(self):
        self.archive.save("thread-test", fixture())
        result = fixture(amount=tokens(160, 40, 20, 10), complete=False)
        result["turns"][0]["quality"] = "partial"
        self.assertFalse(self.archive.save("thread-test", result)["retained"])
        restarted = usage_archive.UsageArchive(self.folder)
        self.assertEqual(restarted.load("thread-test")["result"], result)

    def test_new_turn_usage_partial_scan_is_durable(self):
        self.archive.save("thread-test", fixture())
        result = fixture(complete=False)
        next_turn = deepcopy(result["turns"][0])
        next_turn["id"] = "turn-2"
        result["turns"].append(next_turn)
        self.assertFalse(self.archive.save("thread-test", result)["retained"])
        self.assertEqual(len(self.archive.load("thread-test")["result"]["turns"]), 2)

    def test_initial_partial_scan_is_durable_and_can_grow(self):
        result = fixture(complete=False)
        self.archive.save("thread-test", result)
        self.assertEqual(self.archive.load("thread-test")["result"], result)
        bigger = fixture(amount=tokens(160, 40, 20, 10), complete=False)
        self.assertFalse(self.archive.save("thread-test", bigger)["retained"])

    def test_partial_historical_snapshot_without_segments_preserves_provenance(self):
        result = fixture(complete=False)
        del result["turns"][0]["usageSegments"]
        result.update(sourceScope="historical_snapshot", sourceAt=1791820800)
        result["turns"][0]["quality"] = "partial"
        self.archive.save("thread-test", result)
        self.assertEqual(self.archive.load("thread-test")["result"], result)
        self.assertFalse(self.archive.save("thread-test", fixture())["retained"])
        self.assertEqual(self.archive.load("thread-test")["result"], fixture())

    def test_legacy_snapshot_cannot_erase_existing_increment_timestamps(self):
        self.archive.save("thread-test", fixture())
        legacy = fixture()
        del legacy["turns"][0]["usageSegments"]
        self.assertEqual(self.archive.save("thread-test", legacy)["reason"], "segments_missing")
        self.assertEqual(self.archive.load("thread-test")["result"], fixture())

    def test_identity_tombstone_survives_restart_and_rejects_prefix_revival(self):
        self.archive.save("thread-test", fixture())
        self.archive.invalidate("thread-test", "private-error-must-not-persist")
        archive = usage_archive.UsageArchive(self.folder)
        self.assertIsNone(archive.load("thread-test"))
        self.assertEqual(archive.save("thread-test", fixture(complete=False))["reason"], "identity_rejected")
        self.assertIsNone(archive.load("thread-test"))
        self.assertEqual(archive.stats(), {"savedThreads": 0, "invalidatedThreads": 1})
        self.assertNotIn(b"private-error", archive.path.read_bytes())
        self.assertFalse(archive.save("thread-test", fixture())["retained"])
        self.assertEqual(archive.load("thread-test")["result"], fixture())

    def test_invalidating_never_read_thread_prevents_unavailable_fallback(self):
        self.archive.invalidate("thread-test")
        self.assertIsNone(self.archive.load("thread-test"))

    def test_wrong_checksum_is_rejected_with_content_free_error(self):
        self.archive.save("thread-test", fixture())
        self.mutate_payload(lambda value: value.update(secret="private-corrupt-marker"))
        with self.assertRaisesRegex(usage_archive.UsageArchiveError, "校验失败") as error:
            self.archive.load("thread-test")
        self.assertNotIn("private-", str(error.exception))

    def test_valid_checksum_does_not_override_foreign_thread_identity(self):
        self.archive.save("thread-test", fixture())
        self.mutate_payload(lambda value: value.update(threadId="foreign-thread"), rehash=True)
        with self.assertRaisesRegex(usage_archive.UsageArchiveError, "校验失败"):
            self.archive.load("thread-test")

    def test_checksum_valid_foreign_turn_and_extra_schema_fields_rejected(self):
        for mutation in (lambda value: value["turns"][0].update(threadId="foreign"),
                         lambda value: value.update(extra="private-extra")):
            with self.subTest(mutation=mutation):
                self.archive.invalidate("thread-test")
                self.archive.save("thread-test", fixture())
                self.mutate_payload(mutation, rehash=True)
                with self.assertRaises(usage_archive.UsageArchiveError):
                    self.archive.load("thread-test")

    def test_malformed_json_and_unknown_database_are_safe_errors(self):
        self.archive.save("thread-test", fixture())
        with closing(sqlite3.connect(self.archive.path)) as connection, connection:
            payload = b"private-invalid-json"
            connection.execute("UPDATE snapshots SET payload=?,content_hash=?", (payload, hashlib.sha256(payload).hexdigest()))
        with self.assertRaisesRegex(usage_archive.UsageArchiveError, "校验失败"):
            self.archive.load("thread-test")
        self.archive.path.write_bytes(b"private-non-sqlite")
        with self.assertRaisesRegex(usage_archive.UsageArchiveError, "无法读写"):
            self.archive.load("thread-test")

    def test_invalid_input_and_counter_conservation_rejected(self):
        mutations = [lambda v: v.update(identityVerified=False), lambda v: v.update(threadId="foreign"),
                     lambda v: v["turns"][0]["tokens"].update(total=True),
                     lambda v: v["turns"][0]["usageSegments"][0]["tokens"].update(input=90, total=110),
                     lambda v: v["turns"][0].update(startedAt=float("nan")),
                     lambda v: v["turns"][0].update(startedAt=10**1000),
                     lambda v: v["turns"][0].update(note="\ud800"),
                     lambda v: v["turns"].append(deepcopy(v["turns"][0]))]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result = fixture()
                mutation(result)
                with self.assertRaises(usage_archive.UsageArchiveError):
                    self.archive.save("thread-test", result)
        self.assertFalse(self.archive.path.exists())

    def test_transaction_failure_leaves_prior_snapshot_and_surfaces_error(self):
        self.archive.save("thread-test", fixture())
        with closing(sqlite3.connect(self.archive.path)) as connection, connection:
            connection.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END")
        with self.assertRaisesRegex(usage_archive.UsageArchiveError, "无法读写"):
            self.archive.save("thread-test", fixture(amount=tokens(160, 40, 20, 10)))
        self.assertEqual(self.archive.load("thread-test")["result"], fixture())

    def test_connection_failure_is_visible_without_payload_or_path(self):
        with mock.patch.object(usage_archive.sqlite3, "connect", side_effect=sqlite3.OperationalError("private-path-marker")):
            with self.assertRaises(usage_archive.UsageArchiveError) as error:
                self.archive.save("thread-test", fixture())
        self.assertNotIn("private-", str(error.exception))

    def test_concurrent_first_writers_preserve_all_threads(self):
        def save(index):
            ident = f"thread-{index}"
            return usage_archive.UsageArchive(self.folder).save(ident, fixture(ident))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(save, range(16)))
        self.assertTrue(all(not result["retained"] for result in results))
        self.assertEqual(self.archive.stats()["savedThreads"], 16)

    def test_concurrent_same_task_never_regresses(self):
        self.archive.save("thread-test", fixture())
        def save(index):
            amount = tokens(80 * index, 20 * index, 10 * index, 5 * index)
            return usage_archive.UsageArchive(self.folder).save("thread-test", fixture(amount=amount))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(save, range(1, 17)))
        self.assertEqual(self.archive.load("thread-test")["result"]["turns"][0]["tokens"]["total"], 1600)

    @unittest.skipIf(os.name == "nt", "POSIX permissions and symlink checks")
    def test_owner_only_permissions_and_symlink_refusal(self):
        self.archive.save("thread-test", fixture())
        self.assertEqual(stat.S_IMODE(self.archive.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.archive.path.stat().st_mode), 0o600)
        target = self.folder / "other.sqlite"
        self.archive.path.rename(target)
        self.archive.path.symlink_to(target)
        with self.assertRaises(usage_archive.UsageArchiveError):
            self.archive.load("thread-test")

    def test_size_bound_refuses_snapshot_without_mutating_existing(self):
        self.archive.save("thread-test", fixture())
        with mock.patch.object(usage_archive, "MAX_SNAPSHOT_BYTES", 10):
            with self.assertRaises(usage_archive.UsageArchiveError):
                self.archive.save("thread-test", fixture(amount=tokens(160, 40, 20, 10)))
        self.assertEqual(self.archive.load("thread-test")["result"], fixture())


if __name__ == "__main__":
    unittest.main()
