'use strict';
/**
 * Pure helpers for Codex ↔ AgentProc permission mapping.
 * Shared by bridge.js and unit tests.
 *
 * Wire 0.3: the bridge decides permission mode from `turn.permission`, not
 * from an env var, so there is no permissionEnabled() here.
 */

function buildArgs(message, sessionId, env) {
  const model = (env.CODEX_MODEL || '').trim();
  if (sessionId) {
    const args = ['codex', 'exec', 'resume', '--json', sessionId, message];
    if (model) args.push('-c', `model="${model}"`);
    return args;
  }
  const args = ['codex', 'exec', '--json', message];
  if (model) args.push('-c', `model="${model}"`);
  return args;
}

function buildPermissionArgs(message, sessionId, env) {
  // on-request so PermissionRequest hooks actually fire; bypass hook trust
  // because we inject a one-shot hooks.json under a temp CODEX_HOME.
  const args = buildArgs(message, sessionId, env);
  // Insert flags after `exec` / `exec resume`.
  // buildArgs shapes:
  //   codex exec --json <msg>
  //   codex exec resume --json <id> <msg>
  const insertAt = args[2] === 'resume' ? 3 : 2;
  args.splice(
    insertAt,
    0,
    '--dangerously-bypass-hook-trust',
    '-c',
    'approval_policy="on-request"',
  );
  return args;
}

function parseEvent(event) {
  const etype = event.type;
  if (etype === 'thread.started') {
    return { sessionId: event.thread_id };
  }
  if (etype === 'item.completed') {
    const item = event.item || {};
    if (item.type === 'agent_message') {
      const text = item.text || '';
      return text ? { partialText: text } : null;
    }
    return null;
  }
  if (etype === 'turn.failed') {
    return { error: String(event.error || 'codex turn failed') };
  }
  return null;
}

/** Codex PermissionRequest hook stdin → AgentProc {"type":"permission_request"} payload. */
function hookInputToPermissionRequest(payload, requestId) {
  if (!payload || typeof payload !== 'object') return null;
  const toolInput = (payload.tool_input && typeof payload.tool_input === 'object'
    && !Array.isArray(payload.tool_input))
    ? payload.tool_input
    : {};
  const req = {
    request_id: String(requestId || ''),
    tool_name: String(payload.tool_name || 'tool'),
    input: toolInput,
  };
  if (!req.request_id) return null;
  if (typeof toolInput.description === 'string' && toolInput.description) {
    req.description = toolInput.description;
  }
  if (typeof payload.turn_id === 'string' && payload.turn_id) {
    req.turn_id = payload.turn_id;
  }
  return req;
}

/** AgentProc {"type":"permission_response"} → Codex PermissionRequest hook stdout. */
function permissionResponseToHookOutput(resp) {
  if (resp && resp.behavior === 'allow') {
    return {
      hookSpecificOutput: {
        hookEventName: 'PermissionRequest',
        decision: { behavior: 'allow' },
      },
    };
  }
  const message = (resp && typeof resp.message === 'string' && resp.message.trim())
    ? resp.message
    : 'denied by bridge';
  return {
    hookSpecificOutput: {
      hookEventName: 'PermissionRequest',
      decision: { behavior: 'deny', message },
    },
  };
}

function buildHooksJson(hookScriptPath) {
  // Absolute path; quote-safe for shells that Codex may wrap.
  const command = `python3 ${JSON.stringify(hookScriptPath)}`;
  return {
    hooks: {
      PermissionRequest: [
        {
          matcher: '.*',
          hooks: [
            {
              type: 'command',
              command,
              statusMessage: 'AgentProc permission',
              timeout: 600,
            },
          ],
        },
      ],
    },
  };
}

module.exports = {
  buildArgs,
  buildPermissionArgs,
  parseEvent,
  hookInputToPermissionRequest,
  permissionResponseToHookOutput,
  buildHooksJson,
};
