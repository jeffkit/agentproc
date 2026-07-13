"""SDK entry conformance harness (Python).

Run as: ``python _sdk_harness.py <kind>``

Each ``kind`` maps to a handler that exercises one return / error path of
``create_profile``. The conformance test (``test_sdk.py``) spawns this harness
with a controlled ``AGENT_*`` env per scenario in ``spec/conformance/sdk.json``
and asserts the exact stdout + exit code. The same scenarios run against the
Node SDK (``sdk/node/src/sdk_harness.js``), so the two SDK entries cannot drift.
"""

from __future__ import annotations

import sys

from agentproc import AgentResult, ProtocolError, create_profile


async def async_string(ctx):
    return "hello world"


async def async_result(ctx):
    return AgentResult(response="hi there", session_id="sess-abc")


async def async_none_partial(ctx):
    await ctx.send_partial("streaming chunk")
    return None


async def async_protocol_error(ctx):
    raise ProtocolError("bad thing")


async def async_send_error_then_return(ctx):
    await ctx.send_error("warn")
    return "after"


async def async_partial_with_role(ctx):
    await ctx.send_partial("thinking...", role="thinking")
    return "answer"


def sync_string(ctx):
    return "hello from sync"


def sync_partial_bare(ctx):
    # Bare call (no await) — the write happens at call time.
    ctx.send_partial("sync chunk")
    return "done"


async def async_permission_roundtrip(ctx):
    # Exercises the permission channel end-to-end at the SDK level:
    #   1. handler sends a {"type":"permission_request"} to the bridge
    #   2. handler reads the {"type":"permission_response"} frame the bridge
    #      writes back on stdin
    #   3. handler echoes the decision as the reply body so the conformance
    #      test can assert what the SDK saw.
    ctx.send_permission_request({
        "request_id": "r1",
        "tool_name": "Bash",
        "input": {"command": "echo ok"},
        "description": "echo",
    })
    resp = ctx.read_permission_response()
    behavior = (resp.get("behavior") if isinstance(resp, dict) else None) or "none"
    return f"perm={behavior}"


HANDLERS = {
    "async_string": async_string,
    "async_result": async_result,
    "async_none_partial": async_none_partial,
    "async_protocol_error": async_protocol_error,
    "async_send_error_then_return": async_send_error_then_return,
    "async_partial_with_role": async_partial_with_role,
    "sync_string": sync_string,
    "sync_partial_bare": sync_partial_bare,
    "async_permission_roundtrip": async_permission_roundtrip,
}


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: _sdk_harness.py <kind>\n")
        return 2
    kind = sys.argv[1]
    handler = HANDLERS.get(kind)
    if handler is None:
        sys.stderr.write(f"unknown kind: {kind}\n")
        return 2
    create_profile(handler)
    return 0  # create_profile calls sys.exit; unreachable in practice


if __name__ == "__main__":
    sys.exit(main())
