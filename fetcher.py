import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ── Public GitHub proxy-list repositories (updated frequently) ─────────────────
SOURCES = {
    "TheSpeedX":  "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "clarketm":   "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "monosans":   "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "ShiftyTR":   "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "jetkai":     "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
}

FETCH_TIMEOUT = 15
_IP_PORT_RE   = re.compile(r"^(\d{1,3}\.){3}\d{1,3}:\d{2,5}$")


def _parse_line(raw: str):
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    line = re.sub(r"^\w+://", "", line)     # strip scheme
    line = line.split("/", 1)[0].strip()    # strip path
    if "@" in line:
        line = line.split("@", 1)[1]        # strip credentials
    return line if _IP_PORT_RE.match(line) else None


def _fetch_one(name: str, url: str):
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        found = [p for p in map(_parse_line, resp.text.splitlines()) if p]
        print(f"    [{name:<12}]  {len(found):>5} proxies fetched")
        return found
    except Exception as exc:
        print(f"    [{name:<12}]  FAILED — {exc}")
        return []


def fetch_all_proxies() -> list[str]:
    """Fetch from all sources concurrently. Returns a deduplicated list."""
    print(f"[*] Fetching proxy lists from {len(SOURCES)} sources...")
    all_proxies: list[str] = []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as exe:
        futures = {exe.submit(_fetch_one, name, url): name for name, url in SOURCES.items()}
        for fut in as_completed(futures):
            all_proxies.extend(fut.result())
    unique = sorted(set(all_proxies))
    print(f"[+] {len(unique)} unique proxies collected.\n")
    return unique
