# echo-agent

The smallest valid AgentProc agent (wire 0.4). It reads the `{"type":"turn",...}` object from stdin and writes the message back to stdout as a `{"type":"result"}` event prefixed with "You said: ". Useful for verifying that your messaging bridge speaks the protocol correctly before plugging in a real AI CLI.

Available in three languages — all produce identical output:

| File | Language | Run with |
|------|----------|----------|
| `bridge.py` | Python | `python3 bridge.py` |
| `bridge.js` | Node.js | `node bridge.js` |
| `bridge.sh` | Bash | `bash bridge.sh` |

## Profile

```yaml
command: python3              # argv[0] — a single token
args: ["{{PROFILE_DIR}}/bridge.py"]   # or: node / bash + the bridge script
# cwd intentionally omitted: hub run defaults to your current directory.
timeout_secs: 10
streaming: false
```

## Quick test

```bash
agentproc hub run echo-agent -p "hello"
# → You said: hello
```

## Local test (without the CLI)

<details>
<summary>Drive the script directly</summary>

```bash
cd hub/echo-agent
echo '{"type":"turn","message":"hello","session_id":"","protocol_version":"0.4"}' | python3 bridge.py
# → {"type":"result","text":"You said: hello"}
```

</details>

## When to use

- **Verifying a bridge implementation.** Before testing with claude-code or codex (which cost real API calls), run echo-agent to confirm your bridge correctly writes the turn object and reads the NDJSON reply.
- **Learning the protocol.** The bridge scripts are a few lines each — read them alongside [the spec](https://agentproc.dev/spec/) to see how the contract maps to actual code.
- **CI smoke tests.** A messaging bridge's test suite can spin up echo-agent as a stub agent.

## License

MIT.
