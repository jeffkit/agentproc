// Type declarations for agentproc
// Protocol version: 0.1 (see spec/protocol.md)

declare module 'agentproc' {

  export const PROTOCOL_VERSION: string;

  /** A single attachment on the user message (draft, multi-attachment). */
  export interface Attachment {
    /** Kind of attachment: 'image' | 'file' | 'audio' | 'video'. */
    type: string;
    /** URL the bridge has made available for fetching. */
    url: string;
    /** Optional filename or display name. */
    name?: string;
  }

  /** Input context passed to the agent handler. */
  export interface AgentContext {
    /** User message text (AGENT_MESSAGE). */
    message: string;
    /** Session ID from the previous turn. Empty = new session. */
    sessionId: string;
    /** Human-readable session name (AGENT_SESSION_NAME). */
    sessionName: string;
    /** Sender identifier (AGENT_FROM_USER). */
    fromUser: string;
    /** Whether the bridge expects streaming output. */
    streaming: boolean;
    /** Protocol version the bridge implements. */
    protocolVersion: string;
    /** Image attachment URL. Empty if no image. */
    imageUrl: string;
    /** File attachment URL. Empty if no file. */
    fileUrl: string;
    /** Parsed attachments from AGENT_ATTACHMENTS (draft). Empty array when unset. */
    attachments: Attachment[];

    /** Send a streaming chunk to the user immediately. */
    sendPartial(text: string): void;
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

  /** Parse AGENT_ATTACHMENTS JSON. Returns [] on parse failure. */
  export function parseAttachments(raw: string): Attachment[];

  /** Reject with a user-readable error; surfaced via AGENT_ERROR. */
  export function protocolError(message: string): Promise<never>;
}
