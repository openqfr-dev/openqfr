#!/usr/bin/env python3
import json
from urllib.parse import urlencode
from urllib.request import urlopen

query = urlencode({"symbol": "BTCUSDT", "limit": 3})
with urlopen("https://openqfr.dev/api/v1/failures?" + query, timeout=15) as response:
    result = json.load(response)
print(json.dumps(result, ensure_ascii=False, indent=2))
