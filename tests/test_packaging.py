import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from trigger_guard.cli import main
from trigger_guard.core import load_policy
from trigger_guard.launch import create_isolated_engine, validate_engine_args
from trigger_guard.overlay import digest, prepare_overlay


class PackagingTests(unittest.TestCase):
    def test_demo_blocks_complete_literal_and_emits_one_notice(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["demo", "--chunk-size", "1"]), 0)
        data = json.loads(out.getvalue())
        self.assertNotIn("SECRET", data["visible_text"])
        self.assertIn("SECRE", data["visible_text"])
        self.assertEqual(data["notice_count"], 1)
        self.assertEqual(data["events"][-1], ["finish", "stop"])

    def test_demo_negative_no_notice(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["demo", "--text", "ordinary text"])
        data = json.loads(out.getvalue())
        self.assertEqual(data["visible_text"], "ordinary text")
        self.assertEqual(data["notice_count"], 0)

    def test_mtp_config_mutation_does_not_change_readiness_config(self):
        config = {"engine_args": {"speculative_config": {"method": "mtp"}}}
        before = copy.deepcopy(config)
        def args_factory(**kwargs):
            kwargs["speculative_config"]["target"] = object()
            return kwargs
        result = create_isolated_engine(config, args_factory, lambda value: value)
        self.assertIn("target", result["speculative_config"])
        self.assertEqual(config, before)

    def test_serve_requires_explicit_single_device_and_local_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            for devices in ["", "0,1", "-1"]:
                with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": devices}):
                    with self.assertRaises(ValueError):
                        validate_engine_args({"model": tmp})
            with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "0"}):
                validate_engine_args({"model": tmp})
                with self.assertRaises(ValueError):
                    validate_engine_args({"model": tmp, "tensor_parallel_size": 2})

    def test_overlay_copy_patch_source_preservation_and_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "installed/vllm"
            source.mkdir(parents=True)
            (source / "__init__.py").write_text("old = True\n", encoding="utf-8")
            patches = root / "patches"
            patches.mkdir()
            (patches / "fixture.patch").write_text(
                "--- a/vllm/__init__.py\n+++ b/vllm/__init__.py\n@@ -1 +1 @@\n-old = True\n+new = True\n", encoding="utf-8")
            expected = root / "expected.py"
            expected.write_text("new = True\n", encoding="utf-8")
            data = {"base_commit": "fixture", "patch_order": ["fixture.patch"],
                    "source_files": {"vllm/__init__.py": digest(source / "__init__.py")},
                    "patched_files": {"vllm/__init__.py": digest(expected)}}
            (patches / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
            outer = root / "enclosing-repo"
            subprocess.run(["git", "init", "--quiet", str(outer)], check=True)
            with patch("trigger_guard.overlay.PATCHES", patches):
                output = prepare_overlay(source, outer / "runtime/overlay")
                self.assertEqual((output / "vllm/__init__.py").read_text(), "new = True\n")
                self.assertEqual((source / "__init__.py").read_text(), "old = True\n")
                for invalid in [output, source, source / "nested", root / "installed"]:
                    with self.assertRaises(ValueError):
                        prepare_overlay(source, invalid)
                (source / "__init__.py").write_text("unexpected\n")
                with self.assertRaises(ValueError):
                    prepare_overlay(source, root / "bad-base")
                self.assertFalse((root / "bad-base").exists())

    def test_real_example_policy_validates(self):
        path = Path(__file__).resolve().parents[1] / "examples/policy.json"
        self.assertEqual(len(load_policy(path)["rules"]), 2)
