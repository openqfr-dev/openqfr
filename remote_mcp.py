"""Stateless, read-only MCP Streamable HTTP handler for OpenQFR."""

import json
from urllib.parse import urlparse

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "qfr_search",
        "title": "Search Quant Failure Records",
        "description": "Search public quantitative strategy failure evidence before running a similar backtest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "maxLength": 32},
                "generation": {"type": "string", "maxLength": 64},
                "failure_code": {"type": "string", "maxLength": 64},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}
            },
            "additionalProperties": False
        }
    },
    {
        "name": "qfr_get",
        "title": "Get Quant Failure Record",
        "description": "Retrieve one public QFR record by record ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"record_id": {"type": "string", "pattern": "^qfr:[0-9a-f]{24}$"}},
            "required": ["record_id"],
            "additionalProperties": False
        }
    },
    {
        "name": "qfr_stats",
        "title": "OpenQFR Statistics",
        "description": "Return public record counts and the QFR schema version.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}
    }
]

def tool_result(data):
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": data,
        "isError": False
    }

def call_tool(name, arguments, connect, config):
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "qfr_stats":
        with connect() as conn:
            public = conn.execute("SELECT count(*) FROM records WHERE status='PUBLIC'").fetchone()[0]
        return tool_result({"public_records": public, "schema_version": config["qfr_schema_version"]})
    if name == "qfr_get":
        record_id = str(arguments.get("record_id", ""))
        with connect() as conn:
            row = conn.execute("SELECT payload FROM records WHERE record_id=? AND status='PUBLIC'", (record_id,)).fetchone()
        return tool_result({"found": bool(row), "record": json.loads(row["payload"]) if row else None})
    if name == "qfr_search":
        symbol = str(arguments.get("symbol", ""))[:32]
        generation = str(arguments.get("generation", ""))[:64]
        code = str(arguments.get("failure_code", ""))[:64]
        try:
            limit = min(max(int(arguments.get("limit", 10)), 1), 20)
        except (TypeError, ValueError):
            limit = 10
        clauses, params = ["status='PUBLIC'"], []
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if generation:
            clauses.append("generation=?")
            params.append(generation)
        if code:
            clauses.append("failure_codes LIKE ?")
            params.append('%"' + code + '"%')
        query = "SELECT record_id,logic_fingerprint,symbol,generation,failure_codes FROM records WHERE " + " AND ".join(clauses) + " ORDER BY record_id LIMIT ?"
        with connect() as conn:
            rows = conn.execute(query, params + [limit]).fetchall()
        return tool_result({"matches": [{
            "record_id": row["record_id"],
            "logic_fingerprint": row["logic_fingerprint"],
            "symbol": row["symbol"],
            "generation": row["generation"],
            "failure_codes": json.loads(row["failure_codes"])
        } for row in rows]})
    return None

def handle_mcp(handler, connect, config):
    public = urlparse(config["public_base_url"])
    origin = handler.headers.get("Origin")
    if origin and origin != public.scheme + "://" + public.netloc:
        return handler.json_response(403, {"jsonrpc":"2.0","id":None,"error":{"code":-32001,"message":"Origin not allowed"}})
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if content_type != "application/json":
        return handler.json_response(415, {"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"application/json required"}})
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length < 2 or length > config["max_request_bytes"]:
        return handler.json_response(413, {"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"invalid request size"}})
    try:
        request = json.loads(handler.rfile.read(length))
    except Exception:
        return handler.json_response(400, {"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"Parse error"}})
    request_id = request.get("id") if isinstance(request, dict) else None
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return handler.json_response(400, {"jsonrpc":"2.0","id":request_id,"error":{"code":-32600,"message":"Invalid Request"}})
    if request_id is None:
        handler.send_response(202)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    method = request["method"]
    if method == "initialize":
        requested = request.get("params", {}).get("protocolVersion")
        negotiated = PROTOCOL_VERSION if requested != "2025-03-26" else "2025-03-26"
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "OpenQFR", "version": "0.1.0"},
            "instructions": "Search prior quantitative failure evidence before spending compute on duplicate research."
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params", {})
        result = call_tool(params.get("name"), params.get("arguments", {}), connect, config)
        if result is None:
            return handler.json_response(200, {"jsonrpc":"2.0","id":request_id,"error":{"code":-32602,"message":"Unknown tool"}}, {"MCP-Protocol-Version":PROTOCOL_VERSION})
    else:
        return handler.json_response(200, {"jsonrpc":"2.0","id":request_id,"error":{"code":-32601,"message":"Method not found"}}, {"MCP-Protocol-Version":PROTOCOL_VERSION})
    return handler.json_response(200, {"jsonrpc":"2.0","id":request_id,"result":result}, {"MCP-Protocol-Version":PROTOCOL_VERSION})
