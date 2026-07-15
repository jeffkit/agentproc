'use strict';
/**
 * Built-in executor registry for the AgentProc Node SDK.
 *
 * An executor is a named, in-process implementation of the bridge side of the
 * AgentProc protocol. Instead of spawning a bridge subprocess (which then
 * forks the target CLI), the runner calls the executor directly — eliminating
 * the bridge-process fork overhead while reusing the same buildArgs +
 * parseEvent logic that the standalone bridge scripts use.
 *
 * Each executor entry:
 *
 *   {
 *     cliName:     string   — CLI binary name (for error messages)
 *     installHint: string   — how to install the CLI
 *     plain:       boolean  — true = CLI emits plain text (not NDJSON);
 *                             false (default) = CLI emits NDJSON, use parseEvent
 *     buildArgs:   (message: string, sessionId: string, env: object) => string[]
 *     parseEvent:  (event: object) => ParseResult | null
 *                  (omitted / irrelevant when plain: true)
 *     makeHandlers:  () => { buildArgs, parseEvent }
 *                  — optional factory for stateful executors (e.g. kimi-code,
 *                  cursor) that need fresh per-turn state shared between
 *                  buildArgs and parseEvent. When present, the runner calls
 *                  makeHandlers() once per turn; the returned { buildArgs,
 *                  parseEvent } pair is used for that turn only.
 *                  Executors without makeHandlers use buildArgs / parseEvent
 *                  directly (they must be stateless / re-entrant).
 *   }
 *
 * ParseResult shape (mirrors hub/_shared/stream_utils.js runBridge contract):
 *   {
 *     partialText?: string    — streaming chunk (forwarded via onPartial)
 *     finalText?:   string    — terminal reply body (null = no text body)
 *     sessionId?:   string    — session id to persist
 *     error?:       string    — error message (turn fails)
 *     usage?:       object    — token/cost stats to attach to RunResult
 *   }
 *
 * Hub bridges that use a custom run loop (recursive, echo-agent) are not
 * listed here. Executors listed here cover the subset that uses
 * runBridge / runPlainCli from stream_utils.js (i.e. they have a clean
 * buildArgs + parseEvent pair).
 */

const crypto = require('node:crypto');

// ---------------------------------------------------------------------------
// claude-code
// ---------------------------------------------------------------------------

const claudeCode = {
  cliName: 'claude',
  installHint: 'Install: npm install -g @anthropic-ai/claude-code',
  plain: false,

  buildArgs(message, sessionId, env) {
    const args = [
      'claude', '-p', message,
      '--output-format', 'stream-json',
      '--dangerously-skip-permissions',
    ];
    const disallow = (env.CLAUDE_DISALLOW_TOOLS || 'AskUserQuestion').trim();
    if (disallow) args.push('--disallowed-tools', disallow);
    const model = (env.CLAUDE_MODEL || '').trim();
    if (model) args.push('--model', model);
    if (sessionId) args.push('--resume', sessionId);
    return args;
  },

  parseEvent(event) {
    const etype = event.type;
    if (etype === 'system' && event.subtype === 'init') {
      const sid = event.session_id;
      return (typeof sid === 'string' && sid) ? { sessionId: sid } : null;
    }
    if (etype === 'assistant') {
      const text = (event.message?.content || [])
        .filter(b => b.type === 'text')
        .map(b => b.text)
        .join('');
      return text ? { partialText: text } : null;
    }
    if (etype === 'result') {
      const sessionId = event.session_id;
      if (event.is_error) {
        return { sessionId, error: event.result || 'claude reported an error' };
      }
      return { sessionId, finalText: event.result || null };
    }
    return null;
  },
};

// ---------------------------------------------------------------------------
// codebuddy
// ---------------------------------------------------------------------------

const codebuddy = {
  cliName: 'codebuddy',
  installHint: 'See your internal CodeBuddy installation docs.',
  plain: false,

  buildArgs(message, sessionId, env) {
    const args = [
      'codebuddy', '-p', message,
      '--output-format', 'stream-json',
      '--dangerously-skip-permissions',
    ];
    const disallow = (env.CODEBUDDY_DISALLOW_TOOLS || 'AskUserQuestion').trim();
    if (disallow) args.push('--disallowedTools', disallow);
    const model = (env.CODEBUDDY_MODEL || '').trim();
    if (model) args.push('--model', model);
    if (sessionId) args.push('-r', sessionId);
    return args;
  },

  parseEvent(event) {
    const etype = event.type;
    if (etype === 'assistant') {
      const text = (event.message?.content || [])
        .filter(b => b.type === 'text')
        .map(b => b.text)
        .join('');
      return text ? { partialText: text } : null;
    }
    if (etype === 'result') {
      const sessionId = event.session_id;
      if (event.is_error) {
        return { sessionId, error: event.result || 'codebuddy reported an error' };
      }
      return { sessionId, finalText: event.result || null };
    }
    return null;
  },
};

// ---------------------------------------------------------------------------
// codex
// ---------------------------------------------------------------------------

const codex = {
  cliName: 'codex',
  installHint: 'Install: npm install -g @openai/codex',
  plain: false,

  buildArgs(message, sessionId, env) {
    const model = (env.CODEX_MODEL || '').trim();
    if (sessionId) {
      const args = ['codex', 'exec', 'resume', '--json', sessionId, message];
      if (model) args.push('-c', `model="${model}"`);
      return args;
    }
    const args = ['codex', 'exec', '--json', message];
    if (model) args.push('-c', `model="${model}"`);
    return args;
  },

  parseEvent(event) {
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
  },
};

// ---------------------------------------------------------------------------
// cursor
// ---------------------------------------------------------------------------
// cursor emits a duplicate full-text assistant event at the end of a streamed
// turn; parseEvent must track accumulated text per-turn to suppress it.
// buildArgs is stateless, so only parseEvent uses the per-turn factory.

const cursor = {
  cliName: 'agent',
  installHint: 'Install: brew install cursor-agent  (then run `agent login`)',
  plain: false,

  makeHandlers() {
    const accumulated = [];

    function buildArgs(message, sessionId, env) {
      const args = [
        'agent', '-p', message,
        '--output-format', 'stream-json',
        '--stream-partial-output',
      ];
      if ((env.CURSOR_FORCE || '1') === '1') {
        args.push('--yolo');
      }
      const model = (env.CURSOR_MODEL || '').trim();
      if (model) args.push('--model', model);
      if (sessionId) args.push('--resume', sessionId);
      return args;
    }

    function parseEvent(event) {
      const etype = event.type;
      if (etype === 'system' && event.subtype === 'init') {
        return { sessionId: event.session_id };
      }
      if (etype === 'assistant') {
        const msg = event.message || {};
        const text = (msg.content || [])
          .filter(b => b.type === 'text')
          .map(b => b.text)
          .join('');
        if (!text) return null;
        if (text === accumulated.join('')) return null;
        accumulated.push(text);
        return { partialText: text };
      }
      if (etype === 'result') {
        const sessionId = event.session_id;
        if (event.is_error || event.subtype === 'error') {
          return { sessionId, error: event.result || 'cursor agent reported an error' };
        }
        return { sessionId, finalText: event.result || null };
      }
      return null;
    }

    return { buildArgs, parseEvent };
  },
};

// ---------------------------------------------------------------------------
// gemini-cli
// ---------------------------------------------------------------------------

const geminiCli = {
  cliName: 'gemini',
  installHint: 'Install: npm install -g @google/gemini-cli',
  plain: false,

  buildArgs(message, sessionId, env) {
    const args = [
      'gemini', '-p', message,
      '--output-format', 'stream-json',
      '--yolo',
    ];
    if ((env.GEMINI_SANDBOX || '').trim().toLowerCase() === 'false') {
      args.push('--sandbox', 'false');
    }
    const model = (env.GEMINI_MODEL || '').trim();
    if (model) args.push('--model', model);
    if (sessionId) args.push('--resume', sessionId);
    return args;
  },

  parseEvent(event) {
    const etype = event.type;
    if (etype === 'init') {
      return { sessionId: event.session_id };
    }
    if (etype === 'message') {
      if (event.role !== 'assistant') return null;
      const text = event.content || '';
      if (!text) return null;
      return event.delta ? { partialText: text } : { finalText: text };
    }
    if (etype === 'error') {
      return event.severity === 'error'
        ? { error: event.message || 'gemini reported an error' }
        : null;
    }
    if (etype === 'result' && event.status === 'error') {
      const err = event.error || {};
      return { error: err.message || 'gemini turn failed' };
    }
    return null;
  },
};

// ---------------------------------------------------------------------------
// kimi-code
// ---------------------------------------------------------------------------
// kimi always generates (or reuses) a session id embedded in the CLI args.
// buildArgs generates (or forwards) the id and parseEvent returns it — they
// must share state, so both live inside makeHandlers.

const kimiCode = {
  cliName: 'kimi',
  installHint: 'See https://moonshotai.github.io/kimi-cli for installation',
  plain: false,

  makeHandlers() {
    const session = { id: null };

    function buildArgs(message, sessionId, env) {
      session.id = sessionId || crypto.randomUUID();
      const args = [
        'kimi', '--print', '-p', message,
        '--output-format=stream-json',
        '--session', session.id,
      ];
      const model = (env.KIMI_MODEL || '').trim();
      if (model) args.push('--model', model);
      return args;
    }

    function parseEvent(event) {
      if (event.role === 'assistant') {
        const content = event.content || '';
        if (content) {
          return { partialText: content, finalText: content, sessionId: session.id };
        }
      }
      return null;
    }

    return { buildArgs, parseEvent };
  },
};

// ---------------------------------------------------------------------------
// opencode
// ---------------------------------------------------------------------------

const opencode = {
  cliName: 'opencode',
  installHint: 'Install: npm install -g opencode-ai  (or: curl -fsSL https://opencode.ai/install | bash)',
  plain: false,

  buildArgs(message, sessionId, env) {
    const args = ['opencode', 'run', message, '--auto', '--format', 'json'];
    if (sessionId) args.push('--session', sessionId);
    const model = (env.OPENCODE_MODEL || '').trim();
    if (model) args.push('--model', model);
    return args;
  },

  parseEvent(event) {
    const etype = event.type;
    const sessionId = event.sessionID || null;
    const part = event.part || {};

    if (etype === 'text') {
      const text = part.text || '';
      if (text) return { sessionId, partialText: text };
      return sessionId ? { sessionId } : null;
    }
    if (etype === 'step_start' || etype === 'step_finish' || etype === 'tool_use') {
      return sessionId ? { sessionId } : null;
    }
    if (etype === 'error') {
      const err =
        part.message ||
        (event.error && event.error.message) ||
        'opencode reported an error';
      return { sessionId, error: err };
    }
    return null;
  },
};

// ---------------------------------------------------------------------------
// qwen-code
// ---------------------------------------------------------------------------

const qwenCode = {
  cliName: 'qwen',
  installHint: 'Install: npm install -g @qwen-code/qwen-code',
  plain: false,

  buildArgs(message, sessionId, env) {
    const args = [
      'qwen', '-p', message,
      '--output-format', 'stream-json',
      '--yolo',
    ];
    if ((env.QWEN_SANDBOX || '').trim().toLowerCase() === 'false') {
      args.push('--sandbox', 'false');
    }
    const model = (env.QWEN_MODEL || '').trim();
    if (model) args.push('--model', model);
    if (sessionId) args.push('--resume', sessionId);
    return args;
  },

  parseEvent(event) {
    const etype = event.type;
    if (etype === 'init') {
      return { sessionId: event.session_id };
    }
    if (etype === 'message') {
      if (event.role !== 'assistant') return null;
      const text = event.content || '';
      if (!text) return null;
      return event.delta ? { partialText: text } : { finalText: text };
    }
    if (etype === 'error') {
      return event.severity === 'error'
        ? { error: event.message || 'qwen reported an error' }
        : null;
    }
    if (etype === 'result' && event.status === 'error') {
      const err = event.error || {};
      return { error: err.message || 'qwen turn failed' };
    }
    return null;
  },
};

// ---------------------------------------------------------------------------
// Plain-text bridges (no NDJSON; full stdout is the reply body)
// ---------------------------------------------------------------------------

const agy = {
  cliName: 'agy',
  installHint: 'See the agy project for installation instructions.',
  plain: true,

  buildArgs(message, _sessionId, env) {
    const args = ['agy', '--print', message];
    if ((env.AGY_DANGEROUSLY_SKIP_PERMISSIONS || '1') === '1') {
      args.push('--dangerously-skip-permissions');
    }
    const model = (env.AGY_MODEL || '').trim();
    if (model) args.push('--model', model);
    return args;
  },
};

const aider = {
  cliName: 'aider',
  installHint: 'Install: pip install aider-chat',
  plain: true,

  buildArgs(message, _sessionId, env) {
    const args = [
      'aider',
      '--message', message,
      '--yes-always',
      '--no-show-release-notes',
      '--no-stream',
    ];
    const model = (env.AIDER_MODEL || '').trim();
    if (model) args.push('--model', model);
    return args;
  },
};

const deepseek = {
  cliName: 'deepseek',
  installHint: 'Install from https://deepseek.com/downloads or: brew install deepseek',
  plain: true,

  buildArgs(message, _sessionId, env) {
    const args = ['deepseek', 'exec', '-p', message];
    const model = (env.DEEPSEEK_MODEL || '').trim();
    if (model) args.push('--model', model);
    return args;
  },
};

const pi = {
  cliName: 'pi',
  installHint: 'Install: npm install -g @earendil-works/pi-coding-agent',
  plain: true,

  buildArgs(message, _sessionId, env) {
    const args = ['pi', '-p', message, '--approve'];
    if ((env.PI_NO_EXTENSIONS || '1') !== '0') args.push('--no-extensions');
    const model = (env.PI_MODEL || '').trim();
    if (model) args.push('--model', model);
    return args;
  },
};

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

const EXECUTORS = {
  'claude-code': claudeCode,
  'codebuddy': codebuddy,
  'codex': codex,
  'cursor': cursor,
  'gemini-cli': geminiCli,
  'kimi-code': kimiCode,
  'opencode': opencode,
  'qwen-code': qwenCode,
  'agy': agy,
  'aider': aider,
  'deepseek': deepseek,
  'pi': pi,
};

/**
 * All executor names built into this SDK version.
 * @type {string[]}
 */
const executorNames = Object.keys(EXECUTORS);

module.exports = { EXECUTORS, executorNames };
