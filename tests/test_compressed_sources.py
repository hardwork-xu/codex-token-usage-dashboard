"""Exact-sibling recovery uses synthetic local inputs and never an account."""
from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compressed_sources as cs

THREAD = "synthetic-compressed-thread"
COPY = [sys.executable, "-c", "import sys,shutil;shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)"]


def body(identity=THREAD, tokens=10):
    return (json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n"
            + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "tokens": tokens}})
            + "\n").encode()


class CompressedSourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.original = self.root / "registered.jsonl"
        self.compressed = Path(str(self.original) + ".zst")
        self.cache = cs.CompressedSourceCache(self.root / "private-cache")
        self.decoder = mock.patch.object(cs, "_decoder_command", return_value=COPY)
        self.decoder.start()
        self.addCleanup(self.decoder.stop)

    def write(self, content=None):
        self.compressed.write_bytes(body() if content is None else content)

    def error(self, code, action=None):
        with self.assertRaises(cs.CompressedSourceError) as result:
            (action or (lambda: self.cache.resolve(THREAD, self.original)))()
        self.assertEqual(result.exception.code, code)
        self.assertEqual(str(result.exception), code)
        self.assertEqual(list(self.cache.folder.glob("*.jsonl")), [])
        self.assertEqual(list(self.cache.folder.glob("*.tmp")), [])

    def test_exact_sibling_only_and_existing_plain_never_expanded(self):
        (self.root / "unrelated.jsonl.zst").write_bytes(body())
        self.assertIsNone(self.cache.resolve(THREAD, self.original))
        self.write()
        self.original.write_bytes(body())
        self.assertIsNone(self.cache.resolve(THREAD, self.original))
        self.assertFalse(self.cache.folder.exists())

    def test_private_atomic_cache_reused_across_instances_and_release_is_scoped(self):
        self.write()
        result = self.cache.resolve(THREAD, self.original)
        self.assertEqual(Path(result["path"]).read_bytes(), body())
        self.assertFalse(result["reused"])
        self.assertEqual(result["sourceStamp"], self.cache.inspect(THREAD, self.original))
        self.assertFalse(self.original.exists())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(Path(result["path"]).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.cache.folder.stat().st_mode), 0o700)
        other_original = self.root / "other.jsonl"
        Path(str(other_original) + ".zst").write_bytes(body())
        other = self.cache.resolve(THREAD, other_original)
        recreated = cs.CompressedSourceCache(self.cache.folder)
        with mock.patch.object(cs, "_decode", side_effect=AssertionError("must reuse")):
            self.assertTrue(recreated.resolve(THREAD, self.original)["reused"])
        recreated.release(THREAD, self.original, result["sourceStamp"])
        self.assertFalse(Path(result["path"]).exists())
        self.assertTrue(Path(other["path"]).exists())
        self.assertEqual(self.compressed.read_bytes(), body())

    def test_source_replacement_invalidates_cached_bytes(self):
        self.write()
        first = self.cache.resolve(THREAD, self.original)
        replacement = self.root / "replacement"
        replacement.write_bytes(body(tokens=20))
        os.replace(replacement, self.compressed)
        second = self.cache.resolve(THREAD, self.original)
        self.assertNotEqual(first["sourceStamp"], second["sourceStamp"])
        self.assertFalse(second["reused"])
        self.assertEqual(Path(second["path"]).read_bytes(), body(tokens=20))
        self.cache.release(THREAD, self.original, first["sourceStamp"])
        self.assertTrue(Path(second["path"]).exists())

    def test_foreign_identity_and_invalid_header_never_exposed(self):
        for value in (body("foreign"), b"not-json\n", b'{"type":"event_msg"}\n'):
            self.write(value)
            self.error("identity_mismatch")

    def test_oversized_compressed_decoded_and_header_are_bounded(self):
        self.write()
        with mock.patch.object(cs, "MAX_COMPRESSED_BYTES", 1):
            self.error("decode_limit")
        with mock.patch.object(cs, "MAX_DECODED_BYTES", 1):
            self.error("decode_limit")
        with mock.patch.object(cs, "MAX_HEADER_BYTES", 8):
            self.error("decode_limit")

    def test_decoder_unavailable_keeps_source_and_no_completed_cache(self):
        self.write()
        with mock.patch.object(cs, "_decoder_command", side_effect=cs.CompressedSourceError("decoder_unavailable")):
            self.error("decoder_unavailable")
        self.assertEqual(self.compressed.read_bytes(), body())

    def test_windows_without_optional_decoder_reports_unavailable(self):
        self.decoder.stop()
        try:
            with mock.patch.object(cs.sys, "platform", "win32"), \
                    mock.patch.object(cs.importlib.util, "find_spec", return_value=None), \
                    mock.patch.object(cs.shutil, "which", return_value=None):
                with self.assertRaises(cs.CompressedSourceError) as result:
                    cs._decoder_command()
            self.assertEqual(result.exception.code, "decoder_unavailable")
        finally:
            self.decoder.start()

    def test_decoder_failure_after_valid_output_is_not_accepted(self):
        self.write()
        command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.exit(7)"]
        with mock.patch.object(cs, "_decoder_command", return_value=command):
            self.error("decode_failed")

    def test_decoder_timeout_terminates_process_and_removes_partial_output(self):
        self.write()
        command = [sys.executable, "-c", "import time;time.sleep(30)"]
        with mock.patch.object(cs, "_decoder_command", return_value=command), mock.patch.object(cs, "DECODE_TIMEOUT", 0.1):
            self.error("decode_timeout")

    def test_source_change_during_decode_discards_output(self):
        self.write()
        real_decode = cs._decode
        def decode(*args):
            result = real_decode(*args)
            self.compressed.write_bytes(body(tokens=11))
            return result
        with mock.patch.object(cs, "_decode", side_effect=decode):
            self.error("source_changed")

    def test_modified_cache_and_invalid_manifest_are_not_reused(self):
        self.write()
        first = self.cache.resolve(THREAD, self.original)
        Path(first["path"]).write_bytes(body("foreign"))
        second = self.cache.resolve(THREAD, self.original)
        self.assertFalse(second["reused"])
        self.assertEqual(Path(second["path"]).read_bytes(), body())
        next(self.cache.folder.glob("*.json")).write_text("{}")
        self.assertFalse(self.cache.resolve(THREAD, self.original)["reused"])

    def test_abandoned_owned_temp_is_cleaned_without_touching_other_files(self):
        self.write()
        self.cache._prepare()
        _original, _compressed, key = self.cache._paths(THREAD, self.original)
        abandoned = self.cache.folder / (key + ".abandoned.tmp")
        abandoned.write_text("synthetic only")
        unrelated = self.cache.folder / "unrelated.tmp"
        unrelated.write_text("keep")
        self.cache.resolve(THREAD, self.original)
        self.assertFalse(abandoned.exists())
        self.assertEqual(unrelated.read_text(), "keep")

    def test_symlink_source_and_cache_directory_are_rejected(self):
        actual = self.root / "actual"
        actual.write_bytes(body())
        try:
            self.compressed.symlink_to(actual)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.error("invalid_source")
        self.compressed.unlink()
        self.write()
        other_dir = self.root / "other-cache"
        other_dir.mkdir()
        self.cache.folder.symlink_to(other_dir, target_is_directory=True)
        self.error("invalid_cache")
        self.assertEqual(actual.read_bytes(), body())

    def test_reparse_source_is_rejected_even_if_mode_is_regular(self):
        info = mock.Mock(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_file_attributes=0x400)
        self.assertFalse(cs._regular(info))

    def test_symlink_source_ancestor_is_rejected(self):
        actual = self.root / "real-folder"
        actual.mkdir()
        (actual / "registered.jsonl.zst").write_bytes(body())
        alias = self.root / "alias"
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.error("invalid_source", lambda: self.cache.resolve(THREAD, alias / "registered.jsonl"))

    def test_real_zstd_concatenated_and_truncated_frames(self):
        executable = shutil.which("zstd") or shutil.which("zstd.exe")
        if not executable:
            self.skipTest("optional zstd CLI unavailable")
        raw = body()
        split = raw.index(b"\n") + 1
        def compress(value):
            return subprocess.run([executable, "-q", "-c"], input=value, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, check=True).stdout
        encoded = compress(raw[:split]) + compress(raw[split:])
        self.write(encoded)
        command = [executable, "-d", "-c", "--quiet"]
        with mock.patch.object(cs, "_decoder_command", return_value=command):
            result = self.cache.resolve(THREAD, self.original)
            self.assertEqual(Path(result["path"]).read_bytes(), raw)
            self.cache.release(THREAD, self.original)
            self.write(encoded[:-2])
            self.error("decode_failed")

    def test_available_python_decoder_rejects_truncation_and_reads_concatenation(self):
        exercised = 0
        for module_name, backend in (("compression.zstd", "stdlib"), ("zstandard", "zstandard")):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            exercised += 1
            raw = body()
            split = raw.index(b"\n") + 1
            compress = module.compress if backend == "stdlib" else module.ZstdCompressor().compress
            encoded = compress(raw[:split]) + compress(raw[split:])
            self.write(encoded)
            command = [sys.executable, str(Path(cs.__file__).resolve()), "--decode", backend]
            with mock.patch.object(cs, "_decoder_command", return_value=command):
                result = self.cache.resolve(THREAD, self.original)
                self.assertEqual(Path(result["path"]).read_bytes(), raw)
                self.cache.release(THREAD, self.original)
                self.write(encoded[:-2])
                self.error("decode_failed")
        if not exercised:
            self.skipTest("optional Python Zstandard backends unavailable")


if __name__ == "__main__":
    unittest.main()
