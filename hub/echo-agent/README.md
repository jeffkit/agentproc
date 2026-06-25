# echo-agent

The smallest valid AgentProc agent. Reads `AGENT_MESSAGE` and writes it back to stdout prefixed with "You said: ". Useful for verifying that your messaging bridge speaks the protocol correctly before plugging in a real AI CLI.

Available in three languages — all produce identical output:

| File | Language | Run with |
|------|----------|----------|
| `bridge.py` | Python | `python3 bridge.py` |
| `bridge.js` | Node.js | `node bridge.js` |
| `bridge.sh` | Bash | `bash bridge.sh` |

## Profile

```yaml
command: python3 ./bridge.py          # or: node ./bridge.js / bash ./bridge.sh
cwd: .
timeout_secs: 10
streaming: false
```

## Local test

```bash
cd hub/echo-agent
AGENT_MESSAGE="hello" AGENT_STREAMING="0" python3 bridge.py
# → You said: hello
```

## When to use

- **Verifying a bridge implementation.** Before testing with claude-code or codex (which cost real API calls), run echo-agent to confirm your bridge correctly injects env vars and reads the reply.
- **Learning the protocol.** The bridge scripts are 3–5 lines each — read them alongside [the spec](https://agentproc.dev/spec/) to see how the contract maps to actual code.
- **CI smoke tests.** A messaging bridge's test suite can spin up echo-agent as a stub agent.

## License

MIT.
