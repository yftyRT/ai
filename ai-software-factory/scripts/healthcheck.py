"""Exit 0 only if the factory process AND the model are ready. Usage: python scripts/healthcheck.py [port]"""
import json
import sys
import urllib.request

port = sys.argv[1] if len(sys.argv) > 1 else "8080"
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
        body = json.load(r)
except Exception as exc:  # noqa: BLE001
    print(f"UNHEALTHY: factory not reachable: {exc}")
    sys.exit(2)
print(json.dumps(body, indent=2))
sys.exit(0 if body.get("model_ready") else 1)
