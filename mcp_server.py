#!/usr/bin/env python3
"""Dependency-free, read-only OpenQFR stdio MCP adapter."""

import json
import sys
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://openqfr.dev"
PROTOCOL_VERSION = "2025-06-18"
TOOLS = [
    {"name":"qfr_search","description":"Search public Quant Failure Records before running a similar backtest.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"generation":{"type":"string"},"failure_code":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100}},"additionalProperties":False}},
    {"name":"qfr_get","description":"Retrieve one public Quant Failure Record by ID.","inputSchema":{"type":"object","properties":{"record_id":{"type":"string","pattern":"^qfr:[0-9a-f]{24}$"}},"required":["record_id"],"additionalProperties":False}},
    {"name":"qfr_stats","description":"Return OpenQFR public record counts and schema version.","inputSchema":{"type":"object","properties":{},"additionalProperties":False}},
]

def fetch(path):
    request = Request(BASE + path, headers={"User-Agent":"openqfr-mcp/0.1"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)

def call_tool(name, args):
    args = args if isinstance(args, dict) else {}
    if name == "qfr_stats":
        data = fetch("/api/v1/stats")
    elif name == "qfr_get":
        data = fetch("/api/v1/failures/" + quote(str(args.get("record_id", "")), safe=":"))
    elif name == "qfr_search":
        allowed = {k: args[k] for k in ("symbol","generation","failure_code","limit") if k in args}
        data = fetch("/api/v1/failures?" + urlencode(allowed))
    else:
        raise ValueError("Unknown tool")
    return {"content":[{"type":"text","text":json.dumps(data, ensure_ascii=False)}],"structuredContent":data,"isError":False}

def respond(message):
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        result = {"protocolVersion":PROTOCOL_VERSION,"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"openqfr","version":"0.1.0"},"instructions":"Search prior quantitative failure evidence before spending compute on duplicate research."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools":TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            result = call_tool(params.get("name"), params.get("arguments", {}))
        except (ValueError, HTTPError, URLError, TimeoutError) as exc:
            result = {"content":[{"type":"text","text":"OpenQFR request failed: " + type(exc).__name__}],"isError":True}
    else:
        return {"jsonrpc":"2.0","id":ident,"error":{"code":-32601,"message":"Method not found"}}
    return {"jsonrpc":"2.0","id":ident,"result":result}

def main():
    for line in sys.stdin:
        try:
            message = json.loads(line)
            reply = respond(message)
            if reply is not None:
                print(json.dumps(reply, ensure_ascii=False, separators=(",", ":")), flush=True)
        except Exception:
            print(json.dumps({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}}, separators=(",", ":")), flush=True)

if __name__ == "__main__":
    main()
