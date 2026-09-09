#!/usr/bin/env python3
"""Small, dependency-light OpenQFR HTTP API."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from remote_mcp import handle_mcp

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DB_PATH = ROOT / "data/openqfr.sqlite3"
SCHEMA_PATH = Path("/home/quantadmin/btc_single_batch_v1/qfr_pilot/QFR_SCHEMA_0.1.0.json")
SCHEMA_DRAFT_PATH = ROOT / "schemas/QFR_SCHEMA_0.2.0-draft.json"
AGENT_ID_RE = re.compile(r"^qfr-agent:[0-9a-f]{64}$")
RECORD_ID_RE = re.compile(r"^qfr:[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BLOCKED_KEYS = {"api_key", "password", "secret", "token", "private_key", "ssh_key", "source_code", "executable"}
STARTED_AT = time.time()
LOG = logging.getLogger("openqfr")
MCP_PROTOCOL_VERSION = "2025-06-18"


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS records (
          record_id TEXT PRIMARY KEY,
          logic_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,
          generation TEXT,
          verdict TEXT NOT NULL,
          failure_codes TEXT NOT NULL,
          assurance TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('PUBLIC','PENDING','REJECTED')),
          agent_id TEXT,
          record_sha256 TEXT NOT NULL UNIQUE,
          payload TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS public_logic_unique
          ON records(logic_fingerprint) WHERE status='PUBLIC';
        CREATE TABLE IF NOT EXISTS submissions (
          submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
          record_id TEXT NOT NULL,
          agent_id TEXT NOT NULL,
          body_sha256 TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'PENDING',
          received_at INTEGER NOT NULL,
          UNIQUE(record_id), UNIQUE(body_sha256)
        );
        """)
    import_seed()


def import_seed():
    source = Path(CONFIG["source_records"])
    if not source.exists():
        raise RuntimeError(f"seed source missing: {source}")
    with connect() as conn, source.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            conn.execute(
                """INSERT OR IGNORE INTO records
                   (record_id,logic_fingerprint,symbol,generation,verdict,failure_codes,
                    assurance,status,agent_id,record_sha256,payload,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record["record_id"], record["strategy"]["logic_fingerprint"],
                    record["scope"]["symbol"], record["source"].get("generation"),
                    record["outcome"]["verdict"], json.dumps(record["outcome"]["failure_codes"]),
                    record["evidence"]["assurance"], "PUBLIC", None,
                    record["record_sha256"], canonical(record).decode(), int(time.time()),
                ),
            )


def nested_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key.lower()
            yield from nested_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested_keys(item)


def validate_submission(record):
    errors = []
    required = {"schema", "schema_version", "record_id", "record_type", "source", "scope", "hypothesis", "strategy", "execution_and_validation", "outcome", "evidence", "record_sha256"}
    if not isinstance(record, dict):
        return ["BODY_NOT_OBJECT"]
    if required - set(record):
        errors.append("MISSING_REQUIRED_FIELDS")
    if record.get("schema") != "QFR": errors.append("SCHEMA_NOT_QFR")
    if record.get("schema_version") != CONFIG["qfr_schema_version"]: errors.append("UNSUPPORTED_SCHEMA_VERSION")
    if not RECORD_ID_RE.fullmatch(str(record.get("record_id", ""))): errors.append("INVALID_RECORD_ID")
    if record.get("record_type") != "NEGATIVE_STRATEGY_EVIDENCE": errors.append("INVALID_RECORD_TYPE")
    if record.get("outcome", {}).get("verdict") != "FAIL": errors.append("ONLY_FAILURE_RECORDS_ACCEPTED")
    fp = record.get("strategy", {}).get("logic_fingerprint", "")
    if not SHA256_RE.fullmatch(str(fp)): errors.append("INVALID_LOGIC_FINGERPRINT")
    if BLOCKED_KEYS.intersection(nested_keys(record)): errors.append("FORBIDDEN_SECRET_OR_EXECUTABLE_FIELD")
    claimed = record.get("record_sha256")
    check = dict(record)
    check.pop("record_sha256", None)
    if claimed != hashlib.sha256(canonical(check)).hexdigest(): errors.append("RECORD_HASH_MISMATCH")
    try:
        import jsonschema
        for issue in jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))).iter_errors(record):
            errors.append("JSON_SCHEMA:" + issue.validator)
    except Exception:
        errors.append("SCHEMA_VALIDATOR_ERROR")
    return errors


def verify_signature(agent_id, public_key_b64, signature_b64, body):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public_raw = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_b64, validate=True)
        expected = "qfr-agent:" + hashlib.sha256(public_raw).hexdigest()
        if agent_id != expected or not AGENT_ID_RE.fullmatch(agent_id):
            return False
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, body)
        return True
    except Exception:
        return False


def agent_card():
    base = CONFIG["public_base_url"]
    return {
        "name": "OpenQFR",
        "description": "Search and submit machine-readable Quant Failure Records.",
        "supportedInterfaces": [{"url": base + "/a2a/v1", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}],
        "version": "0.1.0",
        "documentationUrl": base + "/",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {"id": "qfr_search", "name": "Search QFR failures", "description": "Find prior failed quantitative strategies by scope and fingerprint.", "tags": ["quant", "failure", "research", "qfr"], "examples": ["Find BTCUSDT failures with negative net expectancy"]},
            {"id": "qfr_submit", "name": "Submit signed QFR failure", "description": "Submit an Ed25519-signed failure record for quarantine and review.", "tags": ["quant", "failure", "submission", "qfr"], "examples": ["Submit a failed strategy record"]},
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenQFR/0.1"

    def log_message(self, fmt, *args):
        LOG.info("client=%s method=%s path=%s status=%s", self.client_address[0], self.command, self.path.split("?",1)[0], args[1] if len(args)>1 else "-")

    def json_response(self, status, value, extra_headers=None):
        payload = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store" if status >= 400 else "public, max-age=60")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        for key, val in (extra_headers or {}).items(): self.send_header(key, val)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/health":
            with connect() as conn:
                public = conn.execute("SELECT count(*) FROM records WHERE status='PUBLIC'").fetchone()[0]
                pending = conn.execute("SELECT count(*) FROM records WHERE status='PENDING'").fetchone()[0]
            return self.json_response(200, {"status":"ok","service":"openqfr","public_records":public,"pending_records":pending,"uptime_seconds":int(time.time()-STARTED_AT)})
        if path == "/.well-known/agent-card.json":
            return self.json_response(200, agent_card())
        if path == "/schema/qfr/0.1.0":
            return self.json_response(200, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        if path == "/schema/qfr/0.2.0-draft":
            return self.json_response(200, json.loads(SCHEMA_DRAFT_PATH.read_text(encoding="utf-8")))
        if path == "/docs":
            return self.json_response(200, {"title":"OpenQFR quickstart","read_api":CONFIG["public_base_url"] + "/api/v1/failures?symbol=BTCUSDT&limit=3","schema":CONFIG["public_base_url"] + "/schema/qfr/0.1.0","agent_card":CONFIG["public_base_url"] + "/.well-known/agent-card.json","a2a_endpoint":CONFIG["public_base_url"] + "/a2a/v1/message:send","mcp_endpoint":CONFIG["public_base_url"] + "/mcp","source":"https://github.com/openqfr-dev/openqfr"})
        if path == "/openapi.json":
            return self.json_response(200, json.loads((ROOT / "openapi.json").read_text(encoding="utf-8")))
        if path == "/" or path == "/api/v1":
            return self.json_response(200, {"service":"OpenQFR","description":"Machine-readable quantitative failure evidence","schema":"QFR 0.1.0-pilot","records":"/api/v1/failures","stats":"/api/v1/stats","docs":"/docs","openapi":"/openapi.json","agent_card":"/.well-known/agent-card.json","mcp":"/mcp","submissions":"signed records are quarantined; no executable uploads"})
        if path == "/api/v1/stats":
            with connect() as conn:
                rows = conn.execute("SELECT status,count(*) n FROM records GROUP BY status").fetchall()
            return self.json_response(200, {"records":{r["status"].lower():r["n"] for r in rows},"schema_version":CONFIG["qfr_schema_version"]})
        if path == "/api/v1/failures":
            qs = parse_qs(parsed.query)
            try:
                limit = min(max(int(qs.get("limit", [20])[0]), 1), CONFIG["max_page_size"])
                offset = max(int(qs.get("offset", [0])[0]), 0)
            except ValueError:
                return self.json_response(400, {"error":"INVALID_PAGINATION"})
            clauses, params = ["status='PUBLIC'"], []
            for field in ("symbol", "generation"):
                if qs.get(field): clauses.append(field + "=?"); params.append(qs[field][0][:64])
            if qs.get("failure_code"):
                clauses.append("failure_codes LIKE ?"); params.append('%"' + qs["failure_code"][0][:64] + '"%')
            where = " AND ".join(clauses)
            with connect() as conn:
                total = conn.execute("SELECT count(*) FROM records WHERE " + where, params).fetchone()[0]
                rows = conn.execute("SELECT payload FROM records WHERE " + where + " ORDER BY record_id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
            return self.json_response(200, {"total":total,"limit":limit,"offset":offset,"records":[json.loads(r["payload"]) for r in rows]})
        prefix = "/api/v1/failures/"
        if path.startswith(prefix):
            record_id = path[len(prefix):]
            with connect() as conn:
                row = conn.execute("SELECT payload FROM records WHERE record_id=? AND status='PUBLIC'", (record_id,)).fetchone()
            return self.json_response(200, json.loads(row["payload"])) if row else self.json_response(404, {"error":"NOT_FOUND"})
        if path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.json_response(404, {"error":"NOT_FOUND"})

    def do_POST(self):
        if self.path == "/mcp":
            return handle_mcp(self, connect, CONFIG)
        if self.path == "/a2a/v1/message:send":
            return self.handle_a2a_message()
        if self.path != "/api/v1/failures/submit":
            return self.json_response(404, {"error":"NOT_FOUND"})
        if not CONFIG.get("submissions_enabled"):
            return self.json_response(503, {"error":"SUBMISSIONS_DISABLED"})
        if self.headers.get("Content-Type", "").split(";",1)[0].strip() != "application/json":
            return self.json_response(415, {"error":"JSON_REQUIRED"})
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: return self.json_response(400, {"error":"INVALID_CONTENT_LENGTH"})
        if length < 2 or length > CONFIG["max_request_bytes"]:
            return self.json_response(413, {"error":"REQUEST_SIZE_INVALID"})
        body = self.rfile.read(length)
        agent_id = self.headers.get("X-QFR-Agent-ID", "")
        if not verify_signature(agent_id, self.headers.get("X-QFR-Public-Key", ""), self.headers.get("X-QFR-Signature", ""), body):
            return self.json_response(401, {"error":"SIGNATURE_INVALID"})
        try: record = json.loads(body)
        except Exception: return self.json_response(400, {"error":"INVALID_JSON"})
        errors = validate_submission(record)
        if errors: return self.json_response(422, {"error":"QFR_VALIDATION_FAILED","reasons":errors})
        payload = canonical(record).decode()
        body_sha = hashlib.sha256(body).hexdigest()
        try:
            with connect() as conn:
                if conn.execute("SELECT 1 FROM records WHERE logic_fingerprint=?", (record["strategy"]["logic_fingerprint"],)).fetchone():
                    return self.json_response(409, {"error":"DUPLICATE_LOGIC_FINGERPRINT"})
                conn.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    record["record_id"], record["strategy"]["logic_fingerprint"], record["scope"]["symbol"],
                    record.get("source",{}).get("generation"), "FAIL", json.dumps(record["outcome"]["failure_codes"]),
                    "CLAIMED_SIGNED", "PENDING", agent_id, record["record_sha256"], payload, int(time.time())))
                conn.execute("INSERT INTO submissions(record_id,agent_id,body_sha256,received_at) VALUES (?,?,?,?)", (record["record_id"],agent_id,body_sha,int(time.time())))
        except sqlite3.IntegrityError:
            return self.json_response(409, {"error":"DUPLICATE_SUBMISSION"})
        return self.json_response(202, {"status":"PENDING_REVIEW","record_id":record["record_id"],"public":False})

    def handle_a2a_message(self):
        content_type = self.headers.get("Content-Type", "").split(";",1)[0].strip()
        if content_type not in ("application/json", "application/a2a+json"):
            return self.json_response(415, {"error":{"code":415,"status":"UNSUPPORTED_MEDIA_TYPE","message":"application/a2a+json required"}})
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: return self.json_response(400, {"error":{"code":400,"status":"INVALID_ARGUMENT","message":"invalid content length"}})
        if length < 2 or length > CONFIG["max_request_bytes"]:
            return self.json_response(413, {"error":{"code":413,"status":"RESOURCE_EXHAUSTED","message":"request too large"}})
        try: request = json.loads(self.rfile.read(length))
        except Exception: return self.json_response(400, {"error":{"code":400,"status":"INVALID_ARGUMENT","message":"invalid JSON"}})
        message = request.get("message", {}) if isinstance(request, dict) else {}
        operation, args = "help", {}
        for part in message.get("parts", []) if isinstance(message, dict) else []:
            if isinstance(part, dict) and isinstance(part.get("data"), dict):
                operation = part["data"].get("operation", "help")
                args = part["data"]
                break
        if operation == "stats":
            with connect() as conn:
                public = conn.execute("SELECT count(*) FROM records WHERE status='PUBLIC'").fetchone()[0]
            result = {"operation":"stats","publicRecords":public,"schemaVersion":CONFIG["qfr_schema_version"]}
        elif operation == "get":
            record_id = str(args.get("recordId", ""))
            with connect() as conn:
                row = conn.execute("SELECT payload FROM records WHERE record_id=? AND status='PUBLIC'", (record_id,)).fetchone()
            result = {"operation":"get","record":json.loads(row["payload"]) if row else None,"found":bool(row)}
        elif operation == "search":
            symbol = str(args.get("symbol", ""))[:32]
            generation = str(args.get("generation", ""))[:64]
            code = str(args.get("failureCode", ""))[:64]
            try:
                limit = min(max(int(args.get("limit", 20)), 1), CONFIG["max_page_size"])
                offset = max(int(args.get("offset", 0)), 0)
            except (TypeError, ValueError):
                return self.json_response(400, {"error":{"code":400,"status":"INVALID_ARGUMENT","message":"invalid pagination"}})
            clauses, params = ["status='PUBLIC'"], []
            if symbol: clauses.append("symbol=?"); params.append(symbol)
            if generation: clauses.append("generation=?"); params.append(generation)
            if code: clauses.append("failure_codes LIKE ?"); params.append('%"'+code+'"%')
            where = " AND ".join(clauses)
            with connect() as conn:
                total = conn.execute("SELECT count(*) FROM records WHERE " + where, params).fetchone()[0]
                rows = conn.execute("SELECT record_id,logic_fingerprint,symbol,generation,failure_codes FROM records WHERE " + where + " ORDER BY record_id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
            result = {"operation":"search","matches":[{"recordId":r["record_id"],"logicFingerprint":r["logic_fingerprint"],"symbol":r["symbol"],"generation":r["generation"],"failureCodes":json.loads(r["failure_codes"])} for r in rows],"total":total,"limit":limit,"offset":offset}
        else:
            result = {"operation":"help","supportedOperations":["search","get","stats"],"submissionEndpoint":"/api/v1/failures/submit"}
        response = {"message":{"messageId":str(uuid.uuid4()),"contextId":message.get("contextId", str(uuid.uuid4())),"role":"ROLE_AGENT","parts":[{"data":result}]}}
        return self.json_response(200, response, {"Content-Type":"application/a2a+json; charset=utf-8","A2A-Version":"1.0"})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    bind_host = os.environ.get("OPENQFR_BIND_HOST", CONFIG["bind_host"])
    bind_port = int(os.environ.get("OPENQFR_BIND_PORT", CONFIG["bind_port"]))
    server = ThreadingHTTPServer((bind_host, bind_port), Handler)
    LOG.info("OpenQFR listening on %s:%s", bind_host, bind_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
