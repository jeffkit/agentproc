#!/usr/bin/env node
'use strict';
/**
 * Minimal AgentProc echo agent (Node.js).
 *
 * Reads AGENT_MESSAGE and writes it back. No external dependencies, no AI calls.
 * Use this to verify your messaging bridge speaks the protocol correctly.
 */

const message = process.env.AGENT_MESSAGE || '';
process.stdout.write(`You said: ${message}\n`);
process.exit(0);
