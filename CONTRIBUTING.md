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

### Developing against a local `@totalreclaw/core`

All TypeScript client packages (`skill/plugin`, `mcp`, `skill-nanoclaw`) depend on the published `@totalreclaw/core` from npm (currently `^2.0.0`). For day-to-day contribution work this is what you want — `npm install` will pull the released build.

If you are changing Rust code in `rust/totalreclaw-core/` and need to test the resulting WASM bindings in a TypeScript client **before** publishing to npm, use [`npm link`](https://docs.npmjs.com/cli/v10/commands/npm-link) to override the published dep with your local wasm-pack output:

```bash
# 1. Build the WASM package locally
cd rust/totalreclaw-core
./build-wasm.sh

# 2. Register it as a linkable package
cd pkg
npm link

# 3. In each consuming package, override the npm dep with your local link
cd ../../../skill/plugin   # or `mcp/`, or `skill-nanoclaw/`
npm link @totalreclaw/core

# 4. When you're done, restore the published version
npm unlink @totalreclaw/core --no-save
npm install
```

Never commit a `file:` reference to `rust/totalreclaw-core/pkg` in `package.json` or `package-lock.json` — the `pkg/` directory is a build artifact (not checked in), and a `file:` dep inside a published tarball dangles on end-user machines. Prefer the `npm link` workflow above for any local override.

Python contributors can use the equivalent editable install when hacking on PyO3 bindings:

```bash
# From the repo root
cd rust/totalreclaw-core
maturin develop --features python-extension --release
```

This builds the PyO3 extension and installs it into the active virtualenv, shadowing the `totalreclaw-core` package that `pip install totalreclaw` would pull from PyPI.

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
