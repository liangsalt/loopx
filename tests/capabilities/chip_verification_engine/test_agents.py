"""Transport regression tests, distinct from real Codex acceptance runs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopx.capabilities.chip_verification.engine.agents import AgentError, CodexRuntime, PiRuntime, runtime_environment


class AgentTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = Path(__file__).parent / "fixtures" / "fake_agent_transport.py"
        self.real_popen = subprocess.Popen
        self.children = []

    def tearDown(self):
        for process in self.children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.temporary.cleanup()

    def launch(self, kind, mode):
        def wrapper(command, *args, **kwargs):
            if Path(str(command[0])).resolve() == Path(sys.executable).resolve():
                command = [sys.executable, str(self.fixture), kind, mode, *command[1:]]
                process = self.real_popen(command, *args, **kwargs)
                self.children.append(process)
                return process
            return self.real_popen(command, *args, **kwargs)
        return patch("loopx.capabilities.chip_verification.engine.agents.subprocess.Popen", side_effect=wrapper)

    def run_fixture(self, kind="codex", mode="success", schema=None, prompt="A bounded task", timeout=3):
        adapter = CodexRuntime if kind == "codex" else PiRuntime
        with self.launch(kind, mode):
            return adapter(executable=sys.executable, timeout=timeout).run(
                prompt, cwd=self.root, run_dir=self.root / f"{kind}-{mode}", schema=schema, attempt_id="attempt-fixture")

    def test_codex_structured_receipt_session_usage_and_exact_prompt(self):
        schema = {"type": "object"}
        prompt = "Read only the supplied requirement. 中文内容。"
        result = self.run_fixture(schema=schema, prompt=prompt)
        self.assertEqual(result.session_id, "fixture-codex-session")
        self.assertEqual(result.attempt_id, "attempt-fixture")
        self.assertEqual(result.usage, {"input_tokens": 12, "output_tokens": 4})
        self.assertEqual(result.response["prompt_sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        self.assertEqual(result.prompt_chars, len(prompt))
        self.assertIn("read-only", result.command)
        receipt = json.loads((self.root / "codex-success" / "receipt.json").read_text())
        self.assertTrue(receipt["response"]["transport_fixture"])
        self.assertEqual(json.loads((self.root / "codex-success" / "response.schema.json").read_text()), schema)

    def test_adapter_does_not_implicitly_append_workspace_specification(self):
        (self.root / "spec.json").write_text("SOURCE_SENTINEL_NOT_IN_CONTEXT" + "x" * 250000, encoding="utf-8")
        result = self.run_fixture(schema={"type": "object"}, prompt="Only a small context packet")
        self.assertFalse(result.response["marker_present"])
        self.assertLess(result.response["prompt_chars"], 100)
        self.assertEqual((self.root / "codex-success" / "prompt.txt").read_text(), "Only a small context packet")

    def test_codex_missing_empty_invalid_and_failed_process_are_not_success(self):
        for mode, schema in (("missing", None), ("empty", None), ("invalid", {"type": "object"}), ("nonzero", None)):
            with self.subTest(mode=mode), self.assertRaises(AgentError):
                self.run_fixture(mode=mode, schema=schema)
            self.assertFalse((self.root / f"codex-{mode}" / "receipt.json").exists())

    def test_codex_timeout_terminates_process_and_preserves_failure(self):
        with self.assertRaisesRegex(AgentError, "timed out"):
            self.run_fixture(mode="timeout", timeout=0.3)
        self.assertTrue(all(process.poll() is not None for process in self.children))
        failure = json.loads((self.root / "codex-timeout" / "failure.json").read_text())
        self.assertEqual(failure["kind"], "agent_timeout")
        self.assertEqual(failure["attempt_id"], "attempt-fixture")

    def test_reused_attempt_directory_cannot_accept_stale_response(self):
        self.run_fixture(schema={"type": "object"})
        with self.launch("codex", "missing"), self.assertRaises(AgentError):
            CodexRuntime(executable=sys.executable).run("new attempt", cwd=self.root, run_dir=self.root / "codex-success")
        self.assertEqual(len(self.children), 1)

    def test_hard_budget_prevents_any_subprocess_or_attempt_write(self):
        for adapter in (CodexRuntime, PiRuntime):
            target = self.root / adapter.__name__
            with patch("loopx.capabilities.chip_verification.engine.agents.subprocess.Popen") as popen, self.assertRaises(AgentError):
                adapter(executable=sys.executable, max_prompt_chars=20).run("x" * 21, cwd=self.root, run_dir=target)
            popen.assert_not_called()
            self.assertFalse(target.exists())
        with patch("loopx.capabilities.chip_verification.engine.agents.subprocess.Popen") as popen, self.assertRaises(AgentError):
            PiRuntime(executable=sys.executable, max_prompt_chars=20).run("x", cwd=self.root, run_dir=self.root / "schema-overflow", schema={"type": "object"})
        popen.assert_not_called()

    def test_pi_prefers_final_message_and_records_session_reference(self):
        result = self.run_fixture(kind="pi", schema={"type": "object"})
        self.assertTrue(result.response["transport_fixture"])
        self.assertTrue(Path(result.session_id).is_file())
        self.assertEqual(result.attempt_id, "attempt-fixture")
        self.assertIn("--no-tools", result.command)
        self.assertIn("--no-extensions", result.command)
        self.assertIn("--no-skills", result.command)
        self.assertIn("--no-context-files", result.command)
        self.assertTrue(all(process.poll() is not None for process in self.children))

    def test_pi_old_protocol_waits_for_confirmed_idle(self):
        result = self.run_fixture(kind="pi", mode="old_protocol", schema={"type": "object"})
        events = Path(result.transcript).read_text()
        self.assertIn("dvflow-idle", events)
        self.assertTrue(result.response["transport_fixture"])

    def test_pi_acknowledged_or_ended_but_busy_is_not_complete(self):
        with self.assertRaises(AgentError):
            self.run_fixture(kind="pi", mode="still_busy", timeout=0.3)
        self.assertFalse((self.root / "pi-still_busy" / "receipt.json").exists())

    def test_pi_empty_invalid_failed_request_and_early_exit_are_rejected(self):
        for mode in ("empty", "invalid", "request_error", "exit_early"):
            with self.subTest(mode=mode), self.assertRaises(AgentError):
                self.run_fixture(kind="pi", mode=mode, schema={"type": "object"})
            self.assertFalse((self.root / f"pi-{mode}" / "receipt.json").exists())

    def test_runtime_environment_is_a_child_copy_and_preserves_explicit_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://example.invalid:8123", "DVFLOW_TEST": "retained"}, clear=True):
            before = dict(os.environ)
            environment = runtime_environment()
            self.assertEqual(environment["HTTPS_PROXY"], before["HTTPS_PROXY"])
            self.assertEqual(environment["DVFLOW_TEST"], "retained")
            environment["HTTPS_PROXY"] = "changed-only-in-copy"
            self.assertEqual(dict(os.environ), before)

    @unittest.skipUnless(os.name == "nt", "Windows proxy bridge")
    def test_windows_proxy_discovery_never_changes_machine_environment(self):
        import winreg
        with patch.dict(os.environ, {}, clear=True), patch.object(winreg, "OpenKey") as open_key, patch.object(winreg, "QueryValueEx") as query:
            query.side_effect = [(1, 4), ("http=127.0.0.1:8111;https=127.0.0.1:8222", 1)]
            result = runtime_environment()
            self.assertEqual(result["HTTPS_PROXY"], "http://127.0.0.1:8222")
            self.assertEqual(result["NO_PROXY"], "127.0.0.1,localhost")
            self.assertEqual(dict(os.environ), {})
            open_key.assert_called_once()


if __name__ == "__main__":
    unittest.main()
