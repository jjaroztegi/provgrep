# provgrep

Searches published open-source code for what you want to do and returns real snippets, each with
the license it is under, the evidence for that license, and where the code was first observed. A
search website first, with a CLI and an MCP server on the same engine.

## Principles

- A license label is a claim. Each one carries its evidence (file, scope, tool and version) and a
  confidence level whose error rate is measured against ground truth. Licenses are reported as
  detected, never as concluded.
- Code is never shown or copied without its copyright and license notices. No license that lets
  it be shared (none found, proprietary, source-available): link only.
- Say "earliest observed", not "original author", unless the history proves it. Never store or
  show email addresses.
- Every published number ships with the command and corpus version that reproduce it.
- Smallest change that works. No speculative abstractions or "for later" code.
- Docs stay short. Code explains itself; comments say why, not what.

## Environment

Linux and Windows 11. Keep paths, scripts and line endings cross-platform.

## Commits and pull requests

- Conventional commits in plain language: `feat(license): resolve a file's license from its scope`.
- One problem per PR. Body: the problem in a sentence or two, the change, and how it was verified.

## Documentation

- `README.md`: what provgrep is, why, what it found, how to start. `docs/`: how to use it, what is
  unintuitive about it, and every measurement with what reproduces it (`docs/results.md`).
- Before adding a paragraph, ask what a reader would get wrong without it: a finding, a source,
  a constraint, a trap. Do not restate what the code or `--help` shows; link the source.
- Public files never name hosts, addresses, credentials or private notes.
- When something changes, rewrite the affected text instead of appending another account.
