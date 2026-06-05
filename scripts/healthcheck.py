from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/health"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": str(exc.reason)}, ensure_ascii=False))
        return 1

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "error": "invalid json", "body": body[:200]}, ensure_ascii=False))
        return 1

    ok = response.status == 200 and data.get("ok") is True
    print(json.dumps({"ok": ok, "status": response.status, "body": data}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

