"""Process adapters. Agent transcripts are evidence of execution, never DV truth."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    runtime: str
    session_id: str
    attempt_id: str
    returncode: int
    elapsed_seconds: float
    response: Any
    transcript: str
    stderr: str
    prompt_chars: int
    command: list[str]
    usage: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve(executable: str) -> str:
    resolved = shutil.which(executable)
    if resolved:
        return resolved
    path = Path(executable)
    if path.is_file():
        return str(path.resolve())
    raise AgentError(f"Runtime not found: {executable}")


def runtime_environment() -> dict[str, str]:
    """Use the user's enabled Windows proxy for this child only, if not overridden.

    Rust/Node subprocesses do not necessarily consult WinINET settings, unlike
    browsers. Do not edit machine settings or emit proxy credentials to logs.
    """
    env = os.environ.copy()
    if os.name == "nt" and not any(k.upper() in {"HTTPS_PROXY", "ALL_PROXY"} for k in env):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
                value = winreg.QueryValueEx(key, "ProxyServer")[0]
            if enabled and value:
                if "=" in value:
                    mapping = dict(item.split("=", 1) for item in value.split(";") if "=" in item)
                    value = mapping.get("https", mapping.get("http", ""))
                if value:
                    proxy = value if "://" in value else "http://" + value
                    env["HTTPS_PROXY"] = proxy
                    env.setdefault("HTTP_PROXY", proxy)
                    env.setdefault("NO_PROXY", "127.0.0.1,localhost")
        except (OSError, ImportError):
            pass
    return env


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


class CodexRuntime:
    """Start a fresh Codex session for a complete task and optional output schema."""

    def __init__(self, executable: str = "codex", timeout: float = 600,
                 max_prompt_chars: int | None = None):
        self.executable = executable
        self.timeout = timeout
        # Legacy caller parameter. A runtime receives the complete semantic task;
        # model/provider capacity failures remain real execution failures.
        self.max_prompt_chars = None

    def run(self, prompt: str, *, cwd: str | Path, run_dir: str | Path,
            schema: dict | None = None, allow_writes: bool = False,
            attempt_id: str | None = None) -> AgentResult:
        cwd = Path(cwd).resolve()
        target = Path(run_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        if (target / "process.json").exists() or (target / "response.json").exists():
            raise AgentError("Attempt directory was already used; create a fresh run_dir")
        attempt_id = attempt_id or uuid.uuid4().hex
        (target / "prompt.txt").write_text(prompt, encoding="utf-8")
        output = target / "response.json"
        command = [_resolve(self.executable), "exec", "--skip-git-repo-check", "--json",
                   "--color", "never", "--sandbox", "workspace-write" if allow_writes else "read-only",
                   "-C", str(cwd), "-o", str(output)]
        if not allow_writes:
            # Structured workers already receive their complete bounded input.
            # Keep the interactive parent's plugins/tools out of these sessions;
            # this also avoids paying their tool-description context repeatedly.
            # These are invocation-local feature switches, not user config edits.
            for feature in ("plugins", "apps", "multi_agent", "shell_tool", "browser_use",
                            "computer_use", "image_generation", "view_image"):
                command.extend(["--disable", feature])
        if schema is not None:
            schema_path = target / "response.schema.json"
            _write_json(schema_path, schema)
            command.extend(["--output-schema", str(schema_path)])
        command.append("-")
        transcript = target / "events.jsonl"
        error_path = target / "stderr.log"
        started = time.monotonic()
        with transcript.open("w", encoding="utf-8") as stdout, error_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=stdout,
                                       stderr=stderr, text=True, encoding="utf-8",
                                       env=runtime_environment(),
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            _write_json(target / "process.json", {"pid": process.pid, "attempt_id": attempt_id,
                                                   "command": command, "started_at": time.time()})
            try:
                process.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                _terminate(process)
                _write_json(target / "failure.json", {"kind": "agent_timeout", "attempt_id": attempt_id})
                raise AgentError(f"Codex timed out; logs retained at {target}")
        session_id = ""
        usage = None
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id", "")
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
        if process.returncode:
            raise AgentError(f"Codex exit {process.returncode}; inspect {error_path} and {transcript}")
        if not output.is_file():
            raise AgentError(f"Codex returned no final response; inspect {transcript}")
        raw = output.read_text(encoding="utf-8").strip()
        if not raw:
            raise AgentError(f"Codex returned an empty final response: {output}")
        try:
            response = json.loads(raw) if schema else raw
        except json.JSONDecodeError as exc:
            raise AgentError(f"Codex response is not JSON: {output}") from exc
        result = AgentResult("codex", session_id, attempt_id, process.returncode,
                             round(time.monotonic() - started, 3), response, str(transcript),
                             str(error_path), len(prompt), command, usage)
        _write_json(target / "receipt.json", result.to_dict())
        return result


class PiRuntime:
    """Independent Pi RPC worker; the interactive parent remains the user's UI."""

    def __init__(self, executable: str = "pi", timeout: float = 600,
                 max_prompt_chars: int | None = None):
        self.executable, self.timeout, self.max_prompt_chars = executable, timeout, None

    def run(self, prompt: str, *, cwd: str | Path, run_dir: str | Path,
            schema: dict | None = None, allow_writes: bool = False,
            attempt_id: str | None = None) -> AgentResult:
        if schema:
            prompt += "\nReturn only JSON conforming to this schema:\n" + json.dumps(schema)
        target = Path(run_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        if (target / "events.jsonl").exists() or (target / "response.json").exists():
            raise AgentError("Attempt directory was already used; create a fresh run_dir")
        attempt_id = attempt_id or uuid.uuid4().hex
        session_file = target / "pi-session.jsonl"
        command = [_resolve(self.executable), "--mode", "rpc", "--session", str(session_file)]
        # A structured worker needs only its supplied context and output schema.
        # The interactive Pi host keeps its DV extension; child workers must not
        # inherit that tool and recursively launch work or expand the full spec.
        if not allow_writes:
            command.extend(["--no-tools", "--no-extensions", "--no-skills",
                            "--no-prompt-templates", "--no-context-files"])
        (target / "prompt.txt").write_text(prompt, encoding="utf-8")
        events_path, stderr_path = target / "events.jsonl", target / "stderr.log"
        messages: queue.Queue = queue.Queue()
        started = time.monotonic()
        text_fragments: list[str] = []
        final_text = ""
        settled = False
        with stderr_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=Path(cwd).resolve(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=stderr, text=True,
                                       encoding="utf-8", bufsize=1,
                                       env=runtime_environment(),
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

            def reader():
                assert process.stdout
                for line in process.stdout:
                    messages.put(line)
                messages.put(None)

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()

            def send(value):
                assert process.stdin
                process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
                process.stdin.flush()

            send({"id": attempt_id, "type": "prompt", "message": prompt})
            try:
                with events_path.open("w", encoding="utf-8") as events:
                    while time.monotonic() - started < self.timeout:
                        try:
                            line = messages.get(timeout=min(1, self.timeout))
                        except queue.Empty:
                            continue
                        if line is None:
                            break
                        events.write(line)
                        events.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "response" and event.get("success") is False:
                            raise AgentError(f"Pi request failed: {event.get('error')}")
                        if event.get("type") == "message_update":
                            delta = event.get("assistantMessageEvent", {})
                            if delta.get("type") == "text_delta":
                                text_fragments.append(delta.get("delta", ""))
                        if event.get("type") == "message_end":
                            message = event.get("message", {})
                            if message.get("role") == "assistant":
                                final_text = "".join(c.get("text", "") for c in message.get("content", [])
                                                     if c.get("type") == "text")
                        if event.get("type") == "agent_settled":
                            settled = True
                            break
                        if event.get("type") == "agent_end":
                            # Older Pi versions lack agent_settled. Confirm quiescence
                            # through the RPC state response rather than assuming success.
                            send({"id": "dvflow-idle", "type": "get_state"})
                        if event.get("id") == "dvflow-idle" and event.get("type") == "response":
                            state = event.get("data", {})
                            if (state.get("isStreaming") is False and
                                state.get("pendingMessageCount", 0) == 0):
                                settled = True
                                break
                if not settled:
                    raise AgentError(f"Pi did not settle; inspect {events_path}")
            finally:
                _terminate(process)
                thread.join(timeout=2)
                if process.stdin:
                    process.stdin.close()
                if process.stdout:
                    process.stdout.close()
        raw = final_text or "".join(text_fragments)
        if not raw.strip():
            raise AgentError("Pi settled without an assistant result")
        try:
            response = json.loads(raw) if schema else raw
        except json.JSONDecodeError as exc:
            raise AgentError("Pi returned an invalid structured result") from exc
        result = AgentResult("pi", str(session_file), attempt_id, 0,
                             round(time.monotonic() - started, 3), response, str(events_path),
                             str(stderr_path), len(prompt), command)
        _write_json(target / "response.json", response)
        _write_json(target / "receipt.json", result.to_dict())
        return result


def runtime(name: str, **kwargs):
    if name == "codex":
        return CodexRuntime(**kwargs)
    if name == "pi":
        return PiRuntime(**kwargs)
    raise AgentError(f"Unknown agent runtime: {name}")
