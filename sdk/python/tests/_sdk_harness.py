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


HANDLERS = {
    "async_string": async_string,
    "async_result": async_result,
    "async_none_partial": async_none_partial,
    "async_protocol_error": async_protocol_error,
    "async_send_error_then_return": async_send_error_then_return,
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
