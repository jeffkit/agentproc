#!/usr/bin/env python3
"""
Codex PermissionRequest hook → AgentProc bridge relay.

Invoked by `codex` when an approval is needed. Reads the Codex hook input
JSON from stdin, asks the parent bridge over a Unix domain socket
(`AGENTPROC_CODEX_PERM_SOCK`), and prints a PermissionRequest decision
JSON on stdout for Codex.

This script is intentionally standalone (stdlib only) so both the Python
and Node bridges can point Codex at the same file.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import uuid


def _read_stdin() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _build_request(payload: dict) -> dict:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    req = {
        "request_id": str(uuid.uuid4()),
        "tool_name": str(payload.get("tool_name") or "tool"),
        "input": tool_input,
    }
    desc = tool_input.get("description")
    if isinstance(desc, str) and desc.strip():
        req["description"] = desc
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id.strip():
        req["turn_id"] = turn_id
    return req


def _ask_bridge(req: dict, sock_path: str) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(600.0)
        sock.connect(sock_path)
        sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        try:
            sock.close()
        except OSError:
            pass
    line = buf.decode("utf-8", errors="replace").split("\n", 1)[0].strip()
    if not line:
        return {"behavior": "deny", "message": "no permission response from bridge"}
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {"behavior": "deny", "message": "invalid permission response from bridge"}
    return data if isinstance(data, dict) else {
        "behavior": "deny",
        "message": "invalid permission response from bridge",
    }


def _decision_output(resp: dict) -> dict:
    if resp.get("behavior") == "allow":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
    message = resp.get("message")
    if not isinstance(message, str) or not message.strip():
        message = "denied by bridge"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": message},
        }
    }


def main() -> int:
    sock_path = (os.environ.get("AGENTPROC_CODEX_PERM_SOCK") or "").strip()
    if not sock_path:
        # Fail closed: without a bridge socket we must not silently allow.
        sys.stdout.write(json.dumps(_decision_output({
            "behavior": "deny",
            "message": "AGENTPROC_CODEX_PERM_SOCK not set",
        })) + "\n")
        return 0

    payload = _read_stdin()
    req = _build_request(payload)
    try:
        resp = _ask_bridge(req, sock_path)
    except Exception as exc:  # noqa: BLE001 — fail closed to Codex
        resp = {"behavior": "deny", "message": f"permission relay failed: {exc}"}
    sys.stdout.write(json.dumps(_decision_output(resp), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
