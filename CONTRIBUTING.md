# Contributing to CtxWindow

Thanks for considering it. This is a personal-scale open-source project, not
enterprise software, so keep changes proportionate to that.

## Getting set up

```bash
git clone https://github.com/sohaibsohail98/mcp-context-inspector.git
cd mcp-context-inspector
uv venv && uv pip install -e ".[dev]"
source .venv/bin/activate
git config core.hooksPath .githooks   # scans staged changes for secrets before every commit
```

The hooks step needs [gitleaks](https://github.com/gitleaks/gitleaks)
installed (`brew install gitleaks`). Without it, the hook prints a warning
and lets the commit through rather than blocking you, so it degrades safely
if you skip this.

Run the server locally:

```bash
python3 -m mcp_server.server
```

## Before opening a PR

```bash
ruff check .              # lint
python3 -m pytest -v --ignore=api_tests   # unit tests (no network/cloud creds needed)
```

Both run in CI on every push and PR, and a red check means don't merge yet.
`api_tests/` hits a real deployed instance and needs secrets you likely
don't have locally; CI runs it separately and skips gracefully without them.

## Guidelines

- **Keep it simple.** Don't add abstractions, config knobs, or generalization
  for a case that doesn't exist yet. Three similar lines beat a premature
  helper.
- **Comment the why, not the what.** A comment explaining a non-obvious
  constraint, a past bug, or a security tradeoff is welcome. A comment
  restating what the next line obviously does isn't.
- **Add a test with the fix.** Especially for a bug, since a regression test is
  what stops it coming back.
- **Small, focused PRs.** Easier to review, easier to revert if something's
  wrong.

## Reporting a bug

Open an issue with what you expected, what happened, and how to reproduce it.
For a security issue, please don't open a public issue; see below.

## Security

This server handles bearer tokens and OAuth credentials. If you find a
security issue, please report it privately rather than as a public GitHub
issue. Open a
[private security advisory](https://github.com/sohaibsohail98/mcp-context-inspector/security/advisories/new)
instead.
