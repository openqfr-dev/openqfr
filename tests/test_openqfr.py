#!/usr/bin/env python3
import base64, hashlib, http.client, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import openqfr

TEST_PORT = int(os.environ.get("OPENQFR_TEST_PORT", "18787"))


def get(path):
    conn = http.client.HTTPConnection("127.0.0.1", TEST_PORT, timeout=5)
    conn.request("GET", path)
    res = conn.getresponse(); data = json.loads(res.read()); conn.close()
    return res.status, data

def post(path, body, headers):
    conn = http.client.HTTPConnection("127.0.0.1", TEST_PORT, timeout=5)
    conn.request("POST", path, body=body, headers={"Content-Type":"application/json", **headers})
    res = conn.getresponse(); data = json.loads(res.read()); conn.close()
    return res.status, data


def main():
    openqfr.init_db()
    env = dict(os.environ, OPENQFR_BIND_PORT=str(TEST_PORT))
    proc = subprocess.Popen([sys.executable, str(ROOT/"openqfr.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                if get("/health")[0] == 200: break
            except OSError: time.sleep(.1)
        status, health = get("/health")
        assert status == 200 and health["public_records"] == 100
        status, card = get("/.well-known/agent-card.json")
        assert status == 200 and card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        status, page = get("/api/v1/failures?limit=7")
        assert status == 200 and page["total"] == 100 and len(page["records"]) == 7
        status, stats = get("/api/v1/stats")
        assert status == 200 and stats["records"]["public"] == 100
        a2a_body = json.dumps({"message":{"messageId":"test-1","role":"ROLE_USER","parts":[{"data":{"operation":"search","symbol":"BTCUSDT"}}]}}).encode()
        status, a2a = post("/a2a/v1/message:send", a2a_body, {"Content-Type":"application/a2a+json","A2A-Version":"1.0"})
        assert status == 200 and len(a2a["message"]["parts"][0]["data"]["matches"]) == 20
        a2a_limit_body = json.dumps({"message":{"messageId":"test-limit-1","role":"ROLE_USER","parts":[{"data":{"operation":"search","symbol":"BTCUSDT","limit":1,"offset":0}}]}}).encode()
        status, a2a_limit = post("/a2a/v1/message:send", a2a_limit_body, {"Content-Type":"application/a2a+json","A2A-Version":"1.0"})
        limited = a2a_limit["message"]["parts"][0]["data"]
        assert status == 200 and limited["limit"] == 1 and limited["offset"] == 0 and len(limited["matches"]) == 1
        status, rest_limit = get("/api/v1/failures?symbol=BTCUSDT&limit=1&offset=0")
        assert status == 200 and limited["total"] == rest_limit["total"]
        assert limited["matches"][0]["recordId"] == rest_limit["records"][0]["record_id"]
        status, draft = get("/schema/qfr/0.2.0-draft")
        assert status == 200 and draft["properties"]["schema_version"]["const"] == "0.2.0-draft"
        status, bad = get("/api/v1/failures/not-real")
        assert status == 404
        # Create a correctly signed but deliberately new pending record.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        sample = page["records"][0]
        sample["record_id"] = "qfr:" + "1" * 24
        sample["strategy"]["logic_fingerprint"] = "2" * 64
        sample.pop("record_sha256", None)
        sample["record_sha256"] = hashlib.sha256(openqfr.canonical(sample)).hexdigest()
        body = openqfr.canonical(sample)
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        headers = {
          "X-QFR-Agent-ID":"qfr-agent:" + hashlib.sha256(pub).hexdigest(),
          "X-QFR-Public-Key":base64.b64encode(pub).decode(),
          "X-QFR-Signature":base64.b64encode(key.sign(body)).decode(),
        }
        status, accepted = post("/api/v1/failures/submit", body, headers)
        assert status in (202, 409)  # repeatable test leaves the same pending row
        status, duplicate = post("/api/v1/failures/submit", body, headers)
        assert status == 409
        invalid_headers = dict(headers); invalid_headers["X-QFR-Signature"] = base64.b64encode(b"0"*64).decode()
        status, invalid = post("/api/v1/failures/submit", body, invalid_headers)
        assert status == 401
        forbidden = dict(sample); forbidden["api_key"] = "must-not-be-accepted"; forbidden.pop("record_sha256", None)
        forbidden["record_sha256"] = hashlib.sha256(openqfr.canonical(forbidden)).hexdigest()
        forbidden_body = openqfr.canonical(forbidden)
        forbidden_headers = dict(headers); forbidden_headers["X-QFR-Signature"] = base64.b64encode(key.sign(forbidden_body)).decode()
        status, rejected = post("/api/v1/failures/submit", forbidden_body, forbidden_headers)
        assert status == 422 and "FORBIDDEN_SECRET_OR_EXECUTABLE_FIELD" in rejected["reasons"]
        status, stats = get("/api/v1/stats")
        assert stats["records"].get("pending", 0) == 1
        print("READ_API_PASS records=100 pagination=PASS a2a_limit=PASS rest_a2a_parity=PASS schema_draft=PASS agent_card=PASS")
        print("SIGNED_SUBMISSION_PASS quarantine=PASS duplicate=PASS invalid_signature=PASS forbidden_field=PASS")
    finally:
        proc.terminate(); proc.wait(timeout=5)
        with openqfr.connect() as conn:
            conn.execute("DELETE FROM submissions WHERE record_id=?", ("qfr:" + "1" * 24,))
            conn.execute("DELETE FROM records WHERE record_id=? AND status='PENDING'", ("qfr:" + "1" * 24,))


if __name__ == "__main__": main()
