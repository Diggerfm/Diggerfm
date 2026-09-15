"""Shared HTTP helpers for collectors."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_last_call = {}


def throttle(host, min_interval=1.0):
    """Keep at least min_interval seconds between calls to the same host."""
    now = time.time()
    wait = min_interval - (now - _last_call.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def request(url, payload=None, headers=None, timeout=25, retries=3):
    """GET, or POST when payload is given. Returns parsed JSON."""
    host = urllib.parse.urlparse(url).netloc
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    last = None
    for attempt in range(retries):
        throttle(host, 1.0)
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError("request failed after %d attempts: %s" % (retries, last))
