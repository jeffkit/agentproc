#!/usr/bin/env python3
"""
AgentProc bridge for the `codex` CLI (OpenAI Codex, wire 0.3).

Default:
    codex exec --json <message>
    codex exec resume --json <thread_id> <message>

Permission mode (turn.permission is true / profile permission: true):
    Same argv + --dangerously-bypass-hook-trust + approval_policy=on-request,
    with a one-shot CODEX_HOME that installs a PermissionRequest hook.
    The hook relays approvals over a Unix socket ↔
    {"type":"permission_request"} / {"type":"permission_response"} NDJSON.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HUB_DIR not in sys.path:
    sys.path.insert(0, _HUB_DIR)

from _shared.stream_utils import (  # noqa: E402
    EventResult,
    emit,
    emit_error,
    emit_partial,
    emit_session,
    emit_text,
    main_entry,
    read_turn,
)

CLI_NAME = "codex"
INSTALL_HINT = "Install: npm install -g @openai/codex"
HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "permission_hook.py")
_PROFILE_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROFILE_DIR not in sys.path:
    sys.path.insert(0, _PROFILE_DIR)


def build_args(message: str, session_id: str, env) -> list[str]:
    model = env.get("CODEX_MODEL", "").strip()
    if session_id:
        args = [CLI_NAME, "exec", "resume", "--json", session_id, message]
        if model:
            args += ["-c", f'model="{model}"']
        return args
    args = [CLI_NAME, "exec", "--json", message]
    if model:
        args += ["-c", f'model="{model}"']
    return args


def build_permission_args(message: str, session_id: str, env) -> list[str]:
    args = build_args(message, session_id, env)
    insert_at = 3 if len(args) > 2 and args[2] == "resume" else 2
    args[insert_at:insert_at] = [
        "--dangerously-bypass-hook-trust",
        "-c",
        'approval_policy="on-request"',
    ]
    return args


def parse_event(event: dict) -> Optional[EventResult]:
    etype = event.get("type")
    if etype == "thread.started":
        return EventResult(session_id=event.get("thread_id"))
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            text = item.get("text", "")
            return EventResult(partial_text=text) if text else None
        return None
    if etype == "turn.failed":
        return EventResult(error=str(event.get("error") or "codex turn failed"))
    return None


def _build_hooks_json(hook_script: str) -> dict:
    command = f"python3 {json.dumps(hook_script)}"
    return {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "statusMessage": "AgentProc permission",
                            "timeout": 600,
                        }
                    ],
                }
            ]
        }
    }


def _real_codex_home() -> Path:
    from_env = os.environ.get("CODEX_HOME", "").strip()
    if from_env:
        return Path(from_env)
    return Path.home() / ".codex"


def _prepare_permission_home() -> tuple[str, str]:
    tmp = tempfile.mkdtemp(prefix="agentproc-codex-")
    sock_path = os.path.join(tmp, "perm.sock")
    hooks_path = os.path.join(tmp, "hooks.json")
    with open(hooks_path, "w", encoding="utf-8") as fh:
        json.dump(_build_hooks_json(HOOK_SCRIPT), fh, indent=2)
    real_home = _real_codex_home()
    for name in ("auth.json", "config.toml"):
        src = real_home / name
        if src.is_file():
            try:
                shutil.copy2(src, os.path.join(tmp, name))
            except OSError:
                pass
    return tmp, sock_path


def _run_permission_mode(turn: dict, env) -> int:
    message = turn.get("message") if isinstance(turn.get("message"), str) else ""
    session_id = turn.get("session_id") if isinstance(turn.get("session_id"), str) else ""
    attachments = turn.get("attachments") if isinstance(turn.get("attachments"), list) else []

    if not message and not attachments:
        emit_error("turn.message is required (or include turn.attachments)")
        return 1

    tmp, sock_path = _prepare_permission_home()
    waiters: Dict[str, Any] = {}
    waiters_lock = threading.Lock()
    stop_server = threading.Event()

    def handle_client(conn: socket.socket) -> None:
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.decode("utf-8", errors="replace").split("\n", 1)[0].strip()
            if not line:
                return
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                return
            if not isinstance(req, dict):
                return
            rid = req.get("request_id") or ""
            if not isinstance(rid, str) or not rid:
                import uuid
                rid = str(uuid.uuid4())
                req["request_id"] = rid
            event = threading.Event()
            box: Dict[str, Any] = {"event": event, "resp": None}
            with waiters_lock:
                waiters[rid] = box
            emit({"type": "permission_request", **req})
            if not event.wait(timeout=600):
                resp = {
                    "request_id": rid,
                    "behavior": "deny",
                    "message": "permission response timed out",
                }
            else:
                resp = box["resp"] or {
                    "request_id": rid,
                    "behavior": "deny",
                    "message": "no permission response",
                }
            with waiters_lock:
                waiters.pop(rid, None)
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve() -> None:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.exists(sock_path):
                os.unlink(sock_path)
            srv.bind(sock_path)
            srv.listen(8)
            srv.settimeout(0.5)
            while not stop_server.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=handle_client, args=(conn,), daemon=True).start()
        finally:
            try:
                srv.close()
            except OSError:
                pass

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()

    # Bridge stdin ← AgentProc runner ({"type":"permission_response",...} NDJSON)
    def read_bridge_stdin() -> None:
        for raw in sys.stdin:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "permission_response":
                continue
            rid = payload.get("request_id")
            if not isinstance(rid, str) or not rid:
                continue
            with waiters_lock:
                box = waiters.get(rid)
            if box:
                box["resp"] = payload
                box["event"].set()

    stdin_thread = threading.Thread(target=read_bridge_stdin, daemon=True)
    stdin_thread.start()

    child_env = os.environ.copy()
    child_env["CODEX_HOME"] = tmp
    child_env["AGENTPROC_CODEX_PERM_SOCK"] = sock_path
    args = build_permission_args(message, session_id, env)

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        stop_server.set()
        shutil.rmtree(tmp, ignore_errors=True)
        emit_error(f"{CLI_NAME} CLI not found. {INSTALL_HINT}")
        return 1

    stderr_chunks: list[str] = []

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    found_session_id: Optional[str] = None
    last_final_text: Optional[str] = None
    last_partial_text: Optional[str] = None
    error_message: Optional[str] = None

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        result = parse_event(event)
        if result is None:
            continue
        if result.session_id:
            found_session_id = result.session_id
        if result.error:
            error_message = result.error
        if result.partial_text:
            emit_partial(result.partial_text)
            last_partial_text = result.partial_text
        if result.final_text:
            last_final_text = result.final_text

    code = proc.wait()
    stop_server.set()
    with waiters_lock:
        pending = list(waiters.items())
        waiters.clear()
    for rid, box in pending:
        box["resp"] = {
            "request_id": rid,
            "behavior": "deny",
            "message": "no permission response (process ending)",
        }
        box["event"].set()
    shutil.rmtree(tmp, ignore_errors=True)

    if error_message:
        if found_session_id:
            emit_session(found_session_id)
        emit_error(error_message)
        return 1
    if code != 0 and not found_session_id:
        msg = f"{CLI_NAME} exited with {code}"
        s = "".join(stderr_chunks).strip()
        if s:
            msg += f": {s[:500]}"
        emit_error(msg)
        return 1
    if found_session_id:
        emit_session(found_session_id)
    reply_text = last_final_text if last_final_text is not None else last_partial_text
    if reply_text:
        emit_text(reply_text)
    return 0


def main() -> int:
    turn = read_turn()
    if turn.get("permission") is True:
        return _run_permission_mode(turn, os.environ)
    return main_entry(CLI_NAME, INSTALL_HINT, build_args, parse_event, turn=turn)


if __name__ == "__main__":
    sys.exit(main())
