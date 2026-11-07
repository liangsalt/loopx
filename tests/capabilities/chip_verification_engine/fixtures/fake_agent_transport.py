"""Local subprocess protocol fixture. Never used as real model/simulator evidence."""
import hashlib
import json
from pathlib import Path
import sys
import time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

runtime, mode, *arguments = sys.argv[1:]


def emit(event):
    print(json.dumps(event), flush=True)


if runtime == "codex":
    prompt = sys.stdin.read()
    emit({"type": "thread.started", "thread_id": "fixture-codex-session"})
    print("non-JSON diagnostic", flush=True)
    output = Path(arguments[arguments.index("-o") + 1])
    if mode == "timeout":
        time.sleep(20)
    if mode == "nonzero":
        print("fixture failed", file=sys.stderr)
        sys.exit(7)
    if mode == "empty":
        output.write_text("  \n", encoding="utf-8")
    elif mode == "invalid":
        output.write_text("{broken", encoding="utf-8")
    elif mode != "missing":
        output.write_text(json.dumps({"transport_fixture": True, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                                      "prompt_chars": len(prompt), "cwd": str(Path.cwd()), "marker_present": "SOURCE_SENTINEL_NOT_IN_CONTEXT" in prompt}), encoding="utf-8")
    emit({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}})
elif runtime == "pi":
    request = json.loads(sys.stdin.readline())
    prompt = request["message"]
    session = Path(arguments[arguments.index("--session") + 1])
    session.write_text(json.dumps({"type": "session", "id": "fixture-pi-session"}) + "\n", encoding="utf-8")
    emit({"type": "response", "id": request["id"], "command": "prompt", "success": mode != "request_error", "error": "fixture rejection" if mode == "request_error" else None})
    if mode in ("timeout", "request_error"):
        time.sleep(20)
    if mode == "exit_early":
        sys.exit(0)
    result = json.dumps({"transport_fixture": True, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "prompt_chars": len(prompt)})
    if mode == "invalid":
        result = "{broken"
    if mode != "empty":
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "intermediate-not-final"}})
        emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": result}]}})
    if mode == "old_protocol":
        emit({"type": "agent_end"})
        state_request = json.loads(sys.stdin.readline())
        emit({"type": "response", "id": state_request["id"], "success": True, "data": {"isStreaming": False, "pendingMessageCount": 0}})
    elif mode == "still_busy":
        emit({"type": "agent_end"})
        state_request = json.loads(sys.stdin.readline())
        emit({"type": "response", "id": state_request["id"], "success": True, "data": {"isStreaming": False, "pendingMessageCount": 1}})
        # A successful acknowledgement and an ended low-level run are not done.
        time.sleep(20)
    else:
        emit({"type": "agent_settled"})
    time.sleep(20)
