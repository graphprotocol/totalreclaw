# Contributing to TotalReclaw

Thank you for your interest in contributing to TotalReclaw. This guide covers everything you need to get started.

## Getting Started

1. Fork and clone the repository:

```bash
git clone https://github.com/<your-username>/totalreclaw.git
cd totalreclaw
```

2. Install dependencies for the package you plan to work on:

```bash
# Client library
cd client && npm install

# MCP server
cd mcp && npm install

# OpenClaw plugin
cd skill/plugin && npm install

# Self-hosted server (Python)
pip install -r requirements.txt -r server/requirements.txt
```

## Development

### Project Structure

| Directory | Description | Language |
|-----------|-------------|----------|
| `client/` | Core client library (E2EE, LSH, embeddings) | TypeScript |
| `mcp/` | MCP server for Claude Desktop and other hosts | TypeScript |
| `skill/` | OpenClaw plugin | TypeScript |
| `skill-nanoclaw/` | NanoClaw skill package | TypeScript |
| `contracts/` | Solidity smart contracts | Solidity |
| `subgraph/` | The Graph indexer mappings | AssemblyScript |
| `server/` | Self-hosted FastAPI backend | Python |

### Running Tests

```bash
# Client library
cd client && npm test

# OpenClaw plugin
cd skill/plugin && npm test

# MCP server
cd mcp && npm run build

# Self-hosted server
cd server && python -m pytest tests/ -v
```

Run the relevant test suite before submitting any pull request. If your change spans multiple packages, test all affected packages.

## Submitting Changes

1. **Create a branch** from `main`:

```bash
git checkout -b your-branch-name main
```

2. **Make your changes.** Keep commits focused and atomic. Write clear commit messages that explain *why* a change was made, not just what changed.

3. **Write tests** for any new functionality. Bug fixes should include a test that reproduces the issue.

4. **Run tests** to make sure nothing is broken.

5. **Open a pull request** against `main`. In the PR description:
   - Summarize what the change does and why.
   - Note which packages are affected.
   - Describe how you tested it.

6. A maintainer will review your PR. Please be responsive to feedback.

### PR Guidelines

- Keep PRs small and focused. One logical change per PR.
- Do not bundle unrelated fixes or refactors.
- If your change affects user-facing behavior, update the relevant documentation.
- If you add a new feature, update the Feature Compatibility Matrix in `CLAUDE.md`.

## Code Style

- **TypeScript**: Use ESM imports. Follow the existing code conventions in each package.
- **Python**: Follow PEP 8.
- **Formatting**: No trailing whitespace. End files with a newline.
- **Naming**: Use camelCase for TypeScript variables and functions, snake_case for Python.
- **Types**: Prefer explicit types over `any`. Use strict TypeScript where possible.
- **Error handling**: Always handle errors explicitly. Do not silently swallow exceptions.

## Reporting Issues

Open an issue on GitHub with the following information:

- **Description**: What happened and what you expected to happen.
- **Steps to reproduce**: Minimal steps to trigger the issue.
- **Environment**: Node.js version, OS, and which package is affected.
- **Logs or error messages**: Include relevant output (redact any secrets or keys).

Check existing issues before opening a new one to avoid duplicates.

## License

By contributing to TotalReclaw, you agree that your contributions will be licensed under the [MIT License](LICENSE).
