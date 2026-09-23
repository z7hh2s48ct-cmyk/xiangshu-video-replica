"""Container readiness probe through the normal customer ingress boundary."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def main() -> int:
    origin = urlsplit(os.environ.get("VIDEO_REPLICA_PUBLIC_ORIGIN", ""))
    if origin.scheme != "https" or not origin.netloc:
        print("readiness probe: public HTTPS origin is required", file=sys.stderr)
        return 1
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/ready",
        headers={
            "Host": origin.netloc,
            "X-Forwarded-Proto": "https",
            # Distinct from the raw loopback peer: detects accidental Uvicorn rewrites.
            "X-Forwarded-For": "127.0.0.2",
        },
    )
    # Never send the local probe through HTTP_PROXY/HTTPS_PROXY from the host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError):
        print("readiness probe failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
