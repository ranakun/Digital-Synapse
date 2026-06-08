# Security Policy

Digital Synapse is designed for private personal data. Treat vault contents,
source exports, embeddings, and API prompts as sensitive.

## Supported Versions

Security fixes target the latest released major version.

## Reporting Issues

For public repositories, open a GitHub security advisory if available. If that
is not available, open an issue with minimal detail and avoid posting secrets,
private documents, or vault contents.

## Secrets

- API keys must come from `.env` or process environment variables.
- `.env` is gitignored.
- New vaults install a pre-commit hook that blocks common API-key patterns.
- The hook is a safety net, not a substitute for reviewing `git status` and
  `git diff`.

## Remote Providers

Remote LLM extraction sends source text to the configured provider. Users must
choose and acknowledge their provider route before enabling remote calls.
Structured deterministic importers and graph queries do not require remote
providers.

## Private Vaults

Keep private vaults in a separate private repository. If the vault is stored
inside this implementation checkout, use `.\vault`, which is ignored by the
public repo.
