# Security Policy

Agentic Engineering Lab is a public repository. Security and secret hygiene are therefore part of the project architecture, not an afterthought.

## Never commit secrets

Do not commit any of the following, even temporarily:

- API keys;
- OAuth client secrets or refresh tokens;
- session cookies;
- passwords;
- private SSH keys;
- provider access tokens;
- GitHub personal access tokens;
- OpenCode or model-provider credentials;
- cloud infrastructure credentials;
- private webhook secrets;
- private datasets or exported user data;
- private prompts containing sensitive business or personal information;
- internal-only service URLs when they reveal sensitive infrastructure.

If a secret is committed, assume it is compromised even if the commit is later rewritten.

## Configuration

Runtime credentials must be injected through secure local or deployment-specific mechanisms and excluded from version control.

Public examples should use placeholders such as:

```text
OPENAI_API_KEY=replace-me
```

Never publish a real-looking credential as sample data.

## Consumer isolation

ARGUS must not require consumer projects to copy private domain data into this repository.

Integration fixtures should be synthetic or explicitly sanitized.

Digital Assets Lab is the first reference workload, but this public repository must not contain:

- private YouTube credentials;
- channel tokens;
- unpublished media assets unless explicitly intended for public release;
- private analytics exports;
- business secrets from the consumer repository.

## Logging and observability

ARGUS aims to retain detailed execution evidence. That creates an additional security responsibility.

Logs and persisted events should avoid recording raw secrets. Worker inputs and outputs may contain sensitive data and should eventually support explicit redaction or storage policies before ARGUS is used with sensitive workloads.

## External side effects

A retry policy must never assume that a failed local response means the remote operation failed.

For non-idempotent operations, use durable intent and reconciliation before retry. This is both a correctness and security requirement.

## Dependency and execution safety

Future implementations should:

- pin or constrain dependencies appropriately;
- validate external structured inputs;
- bound subprocess execution time;
- terminate process trees on timeout;
- avoid shell interpolation of untrusted values;
- isolate worker execution where the workload risk justifies it;
- treat generated commands and code as untrusted until validated by the relevant execution policy.

## Reporting a vulnerability

Until a dedicated private security-reporting channel is configured, avoid posting exploit details or credentials in public issues. Contact the repository owner privately through an appropriate GitHub-supported channel when possible.
