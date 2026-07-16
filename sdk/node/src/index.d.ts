// Type declarations for agentproc
// Protocol version: 0.4 (see spec/protocol.md)

declare module 'agentproc' {

  export const PROTOCOL_VERSION: string;

  /** Input context passed to the agent handler. */
  export interface AgentContext {
    /** User message text (turn.message). */
    message: string;
    /** Session ID from the previous turn. Empty = new session. */
    sessionId: string;
    /** Human-readable session name (turn.session_name). */
    sessionName: string;
    /** Protocol version the bridge implements (turn.protocol_version). */
    protocolVersion: string;
    /** Attachment list (turn.attachments). Empty array = no attachments. */
    attachments: Array<{ kind: string; url: string; [key: string]: unknown }>;
    /** True when the bridge enabled the optional permission channel. */
    permission: boolean;

    /** Send a streaming chunk to the user immediately. Optional `role` ("output" | "thinking"). */
    sendPartial(text: string, role?: string): void;
    /** Send an error message to the user. Honored regardless of streaming mode. */
    sendError(text: string): void;
  }

  /** Return value from the agent handler. */
  export interface AgentResult {
    /** Final reply text. */
    response?: string;
    /** Session ID to persist. */
    sessionId?: string;
  }

  /** One entry in a session's conversation history. */
  export interface HistoryEntry {
    role: string;
    content: string;
    timestamp: string;
  }

  /** Run a handler as an AgentProc-compliant process. */
  export function createProfile(
    handler: (ctx: AgentContext) => Promise<AgentResult | string | void>
  ): void;

  /** Load conversation history for a session. Returns [] if sessionId is empty. */
  export function loadHistory(sessionId: string, sessionDir?: string): HistoryEntry[];

  /** Append entries to a session's JSONL history file. No-op if sessionId is empty. */
  export function appendHistory(
    sessionId: string,
    entries: Array<{ role: string; content: string; ts?: string }>,
    sessionDir?: string
  ): void;

  /** Resolve the JSONL history file path for a session. Throws if sessionId is empty. */
  export function sessionFilePath(sessionId: string, sessionDir?: string): string;

  /** Reject with a user-readable error; surfaced via a {"type":"error"} event.
   *  Throw the returned instance (or a `ProtocolError`) from a handler. */
  export class ProtocolError extends Error {
    isProtocolError: boolean;
  }

  /** Construct a ProtocolError to throw from a handler. */
  export function protocolError(message: string): ProtocolError;
}
