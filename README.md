# provgrep

Search open-source code by what it does and get back the real snippet: the license it is under
with the evidence for it, where the code was first observed, and an attribution to paste with it.
A search website, with a CLI and an MCP server on the same engine.

Status: early. Licenses are resolved and measured; search does not exist yet.

## Licenses, measured

Every code file gets a license from its own header, the nearest license file above it, or the
nearest package manifest, with the evidence and a level for how much of it agrees (A: the file's
header; B: license file and manifest agree; C: one source, or they disagree; X: nothing found).

Against the licenses Debian maintainers record in `debian/copyright` for 300 Python source
packages from GitHub upstreams (19,534 code files, Debian snapshot 20261024T000000Z): of 15,555
files a permissive filter would show, 74 are under a license Debian does not record as
permissive. Each of the 74 was read by hand. Debian's record is stale for 30 (upstream
relicensed since) and coarse for 44 (the files carry their own notices); provgrep is wrong for
none. Files where both sides agree were not read. The audit, with its reasons and sources:
[truth/debian-python-audit.json](truth/debian-python-audit.json).

Needs [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv run provgrep license PATH       # the license of every code file under PATH
uv run provgrep evaluate licenses --jobs 8  # the measurement above: ~630 MB of downloads, ~1 min
```

Licenses are as detected, with their evidence. They are not legal advice.
