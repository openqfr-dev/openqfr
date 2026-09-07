# OpenQFR

OpenQFR is a machine-readable library of **failed quantitative strategy evidence**. It helps research agents check whether a similar idea already failed before spending compute on another backtest.

> QFR records are negative evidence, not investment advice. OpenQFR does not provide trading signals, place orders, or accept executable uploads.

## Live endpoints

- REST API: `https://openqfr.dev/api/v1`
- QFR records: `https://openqfr.dev/api/v1/failures`
- Schema: `https://openqfr.dev/schema/qfr/0.1.0`
- A2A Agent Card: `https://openqfr.dev/.well-known/agent-card.json`
- A2A endpoint: `https://openqfr.dev/a2a/v1/message:send`

## Five-minute REST example

```bash
curl -s 'https://openqfr.dev/api/v1/failures?symbol=BTCUSDT&limit=3'
```

```python
from urllib.parse import urlencode
from urllib.request import urlopen
import json

query = urlencode({"symbol": "BTCUSDT", "limit": 3})
with urlopen(f"https://openqfr.dev/api/v1/failures?{query}") as response:
    failures = json.load(response)
print(failures["total"])
```

## A2A example

```bash
curl -s https://openqfr.dev/a2a/v1/message:send \
  -H 'Content-Type: application/a2a+json' \
  -H 'A2A-Version: 1.0' \
  --data '{"message":{"messageId":"demo-1","role":"ROLE_USER","parts":[{"data":{"operation":"search","symbol":"BTCUSDT"}}]}}'
```

Supported operations are `search`, `get`, and `stats`.

## MCP

The public, stateless Streamable HTTP endpoint is:

`https://openqfr.dev/mcp`

It exposes read-only `qfr_search`, `qfr_get`, and `qfr_stats` tools. The repository also includes a dependency-free stdio adapter for clients that do not support remote MCP.

```bash
python3 mcp_server.py
```

Example client configuration:

```json
{
  "mcpServers": {
    "openqfr": {
      "command": "python3",
      "args": ["/absolute/path/to/openqfr/mcp_server.py"]
    }
  }
}
```

## Submission safety

Submission headers and review rules are documented in [SUBMISSIONS.md](SUBMISSIONS.md).

Signed submissions are quarantined for review. They must conform to QFR `0.1.0-pilot`, use an Ed25519 signature, contain only failure evidence, and must not contain secrets, credentials, personal data, source code, or executable content. Public records are read-only.

## Project status

This is an early pilot seeded with 100 BTCUSDT quantitative failure records. The schema may change before 1.0. The next milestone is a small closed alpha with independent agent developers.
