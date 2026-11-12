# @totalreclaw/skill-nanoclaw

The TotalReclaw skill for [NanoClaw](https://github.com/qwibitai/nanoclaw). It gives NanoClaw agents end-to-end encrypted memory that persists across sessions and works with your other agents (OpenClaw, Hermes, Claude Code and other MCP hosts).

Memories are encrypted inside the NanoClaw container before they leave it. The TotalReclaw relay and the chain only ever see ciphertext and hashed search tokens.

## Who should install this

NanoClaw operators. If you use a different agent, install its integration instead:

- OpenClaw: [`@totalreclaw/totalreclaw`](https://www.npmjs.com/package/@totalreclaw/totalreclaw)
- Claude Code, Claude Desktop, Cursor, other MCP hosts: [`@totalreclaw/mcp-server`](https://www.npmjs.com/package/@totalreclaw/mcp-server)
- Hermes: [`totalreclaw`](https://pypi.org/project/totalreclaw/) on PyPI

## How it works

The skill swaps NanoClaw's container agent-runner for a TotalReclaw version (`mcp/nanoclaw-agent-runner.ts`). That runner starts [`@totalreclaw/mcp-server`](https://www.npmjs.com/package/@totalreclaw/mcp-server) as a stdio MCP server and allows the agent to call its tools (`mcp__totalreclaw__*`). The agent gets the same tools as any other MCP host: `totalreclaw_remember`, `totalreclaw_recall`, `totalreclaw_forget`, `totalreclaw_export`, `totalreclaw_status`, `totalreclaw_pair` and the rest. The full list is in the [MCP server README](https://github.com/graphprotocol/totalreclaw/blob/main/mcp/README.md#available-tools).

Each NanoClaw group folder becomes its own TotalReclaw namespace (`main`, `work`, ...), so memories stored in one group are not visible from another.

## Install

```bash
npm install @totalreclaw/skill-nanoclaw
```

NanoClaw's skill loader applies the skill from `manifest.yaml` (skill name `add-totalreclaw`). It replaces `container/agent-runner/src/index.ts` with the TotalReclaw runner and adds `@totalreclaw/mcp-server` to the container. The runner starts the MCP server with `npx @totalreclaw/mcp-server`, or with `node $TOTALRECLAW_MCP_PATH` if you point that variable at an installed copy.

The first start of the MCP server downloads a local embedding model, so allow extra time. It is cached after that.

## Set up your account

### Recommended: pair from your browser

Once the skill is running, ask your agent to set up TotalReclaw. It calls `totalreclaw_pair` and gives you a URL and a PIN. Open the URL in your browser and create a new recovery phrase there, or import one you already use with another TotalReclaw agent. The phrase stays in your browser; it never goes through the chat or the agent.

Pairing saves the account to `~/.totalreclaw/credentials.json` inside the container. Keep that path on a persistent volume, or you will need to pair again whenever the container is recreated.

### Advanced: headless deployments

For a deployment where nobody can open a pairing link, the runner also passes `TOTALRECLAW_RECOVERY_PHRASE` from the container environment to the MCP server. Get the phrase out-of-band (from another TotalReclaw agent you paired, or by running `npx @totalreclaw/mcp-server setup` yourself in your own terminal, never through an agent), and inject it from your platform's secret store rather than a committed `.env` file. Anyone who can read that variable can decrypt your memories. The runner strips it from the environment of the agent's Bash commands.

| Variable | Purpose | Default |
|----------|---------|---------|
| `TOTALRECLAW_RECOVERY_PHRASE` | Headless only: the account's recovery phrase | unset (use pairing) |
| `TOTALRECLAW_SERVER_URL` | Relay URL; change it only for a self-hosted server | `https://api.totalreclaw.xyz` |
| `TOTALRECLAW_MCP_PATH` | Path to an installed `@totalreclaw/mcp-server` entry file | unset (uses `npx`) |

## Check that it works

Ask the agent "Do you have access to TotalReclaw memory tools?" and then "What's my TotalReclaw status?". The status answer shows your tier and how much of the monthly quota you have used.

## Library exports

The package also exports the building blocks the skill is made of, for custom runners: the lifecycle hooks `beforeAgentStart`, `agentEnd` and `preCompact` (they take a `TotalReclaw` client from [`@totalreclaw/client`](https://www.npmjs.com/package/@totalreclaw/client)), the extraction prompts and validators, and billing and first-run helpers. See the TypeScript declarations in `dist/`.

## Links

- [NanoClaw getting-started guide](https://github.com/graphprotocol/totalreclaw/blob/main/docs/guides/nanoclaw-getting-started.md)
- Repository: <https://github.com/graphprotocol/totalreclaw> (source in `skill-nanoclaw`)
- Website: <https://totalreclaw.xyz>
- Issues: <https://github.com/graphprotocol/totalreclaw/issues>

## License

MIT
