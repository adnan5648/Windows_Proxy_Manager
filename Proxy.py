"""
Proxy.py — Automatically fetch, test, apply, and rotate free proxies on Windows.

Usage:
    python Proxy.py              # fetch proxies, test them, apply to system
    python Proxy.py --disable    # disable system proxy and exit

Requires Administrator rights for full WinHTTP (netsh) coverage.
WinInet (IE / Edge / Chrome / most apps) works without admin rights.
"""

import argparse
import ctypes
import subprocess
import threading
import time
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from fetcher import fetch_all_proxies

# ── Configuration ──────────────────────────────────────────────────────────────
TARGET_URL        = "https://httpbin.org/ip"  # used to verify proxy is alive
CHECK_TIMEOUT     = 6        # seconds — per-proxy test timeout
MAX_LATENCY_MS    = 3000     # ms  — drop proxy if response time exceeds this
MAX_WORKERS       = 50       # concurrent threads during bulk testing
ROTATE_INTERVAL   = 300      # seconds — planned rotation interval (5 min)
RELOAD_INTERVAL   = 3600     # seconds — re-fetch from online sources (1 hr)
HEALTH_INTERVAL   = 30       # seconds — health-check polling interval
MAX_INITIAL_TEST  = 300      # limit initial testing to first 300 proxies
ACTIVE_PROXIES_FILE = "active_proxies.txt"  # live proxies file for Windows functions
WORKING_LIST_FILE = "working_proxies.txt"   # legacy file (kept for compatibility)


# ── Shared state (touched by main thread + health-check thread) ────────────────
class _State:
    pool:           list[str]       = []
    active:         list[str]       = []       # live proxies from background testing
    rotate_index:   int             = 0
    current:        str | None      = None
    last_reload:    float           = 0.0
    failover:       threading.Event = threading.Event()  # set → switch now
    lock:           threading.Lock  = threading.Lock()


_s = _State()


# ── Proxy testing ──────────────────────────────────────────────────────────────

def _test(host_port: str) -> tuple[str | None, float | None]:
    """
    Returns (host_port, latency_ms) when the proxy responds with HTTP 200.
    Returns (None, None) on any failure.
    """
    url = f"http://{host_port}"
    prx = {"http": url, "https": url}
    try:
        t0 = time.monotonic()
        r  = requests.get(TARGET_URL, proxies=prx, timeout=CHECK_TIMEOUT)
        ms = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            return host_port, ms
    except Exception:
        pass
    return None, None


def test_proxies_sequentially(candidates: list[str]) -> tuple[str | None, int]:
    """
    Test proxies sequentially until finding one that works.
    Returns (working_proxy, index_where_found).
    """
    if not candidates:
        return None, 0

    for i, proxy in enumerate(candidates):
        print(f"[*] Testing [{i+1}/{len(candidates)}]: {proxy}")
        hp, ms = _test(proxy)
        if hp:
            print(f"[+] Found working proxy: {proxy} ({ms:.0f} ms)")
            return hp, i

    print(f"[!] No working proxies found in batch")
    return None, len(candidates)


def verify_remaining_proxies(candidates: list[str]) -> list[str]:
    """
    Concurrently test remaining proxies after initial sequential test.
    Returns working proxies sorted fastest-first.
    Saves results to files.
    """
    if not candidates:
        return []

    print(f"[~] Background: testing {len(candidates)} remaining proxies...")
    working: list[tuple[str, float]] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        futures = {exe.submit(_test, c): c for c in candidates}
        for fut in as_completed(futures):
            hp, ms = fut.result()
            if hp:
                working.append((hp, ms))

    working.sort(key=lambda x: x[1])
    proxies = [hp for hp, _ in working]

    with _s.lock:
        # Merge newly found with existing active proxies
        merged = list(set(_s.active) | set(proxies))
        merged.sort()
        _s.active = merged
        _save_active_proxies(_s.active)

    print(f"[+] Background test complete: {len(proxies)} new working proxies found")
    print(f"[+] Total active proxies: {len(_s.active)}")
    return proxies


def retest_working_proxies() -> None:
    """
    Retest all known working proxies to verify they still work.
    Called after initial batch testing is complete.
    """
    with _s.lock:
        to_test = _s.active.copy() if _s.active else []

    if not to_test:
        print("[!] No working proxies to retest")
        return

    print(f"[*] Retesting {len(to_test)} working proxies...")
    still_working: list[tuple[str, float]] = []

    with ThreadPoolExecutor(max_workers=20) as exe:
        futures = {exe.submit(_test, p): p for p in to_test}
        for fut in as_completed(futures):
            hp, ms = fut.result()
            if hp:
                still_working.append((hp, ms))

    still_working.sort(key=lambda x: x[1])
    verified = [hp for hp, _ in still_working]

    with _s.lock:
        _s.active = verified
        _save_active_proxies(_s.active)

    print(f"[+] Retest complete: {len(verified)} proxies still working")
    if len(verified) < len(to_test):
        died = len(to_test) - len(verified)
        print(f"[!] {died} proxies are now dead")


def _save_active_proxies(proxies: list[str]) -> None:
    """Save active proxies to file for Windows functions to access."""
    with open(ACTIVE_PROXIES_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(p + "\n" for p in proxies)


def _load_active_proxies() -> list[str]:
    """Load active proxies from file."""
    try:
        with open(ACTIVE_PROXIES_FILE, "r", encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def _save_working_list(proxies: list[str]) -> None:
    """Save working proxies (legacy file for compatibility)."""
    with open(WORKING_LIST_FILE, "w", encoding="utf-8") as fh:
        fh.writelines(p + "\n" for p in proxies)


# ── Windows registry / WinHTTP helpers ────────────────────────────────────────

def _refresh_wininet() -> None:
    wi = ctypes.windll.Wininet
    wi.InternetSetOptionW(0, 39, 0, 0)   # INTERNET_OPTION_SETTINGS_CHANGED
    wi.InternetSetOptionW(0, 37, 0, 0)   # INTERNET_OPTION_REFRESH


def _reg_enable(host_port: str) -> None:
    path   = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    server = f"http={host_port};https={host_port}"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable",   0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "ProxyServer",   0, winreg.REG_SZ,    server)
        winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ,    "<local>")
    _refresh_wininet()


def _reg_disable() -> None:
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    _refresh_wininet()


def _winhttp_set(host_port: str) -> None:
    cmd = [
        "netsh", "winhttp", "set", "proxy",
        f"proxy-server=http={host_port};https={host_port}",
        "bypass-list=localhost;127.0.0.1;<local>",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print("[!] WinHTTP update failed — run as Administrator for full system coverage.")


def _winhttp_reset() -> None:
    subprocess.run(
        ["netsh", "winhttp", "reset", "proxy"],
        capture_output=True, text=True, check=False,
    )


def apply_proxy(host_port: str) -> None:
    """
    Apply proxy atomically: WinInet registry first (instant effect for most apps),
    then WinHTTP.  New proxy is active before old one is discarded, so there is
    no gap where traffic has no proxy.
    """
    _reg_enable(host_port)   # takes effect immediately for WinInet apps
    _winhttp_set(host_port)  # WinHTTP / background services
    _s.current = host_port
    print(f"[+] Active proxy  →  {host_port}")


def disable_proxy() -> None:
    _reg_disable()
    _winhttp_reset()
    _s.current = None
    print("[+] System proxy disabled.")


# ── Health monitor for current proxy ─────────────────────────────────────

def _health_thread() -> None:
    """
    Runs as a daemon thread.
    Every HEALTH_INTERVAL seconds it tests the active proxy.
    Sets _s.failover if the proxy is dead or too slow — the main loop
    wakes up instantly and switches to the next proxy.
    """
    while True:
        time.sleep(HEALTH_INTERVAL)
        proxy = _s.current
        if not proxy:
            continue

        hp, ms = _test(proxy)
        if hp is None:
            print(f"[!] Health FAIL  →  {proxy}  (no response)  — triggering failover")
            _s.failover.set()
        elif ms > MAX_LATENCY_MS:
            print(f"[!] Health SLOW  →  {proxy}  ({ms:.0f} ms > {MAX_LATENCY_MS} ms)  — triggering failover")
            _s.failover.set()
        else:
            print(f"[~] Health OK    →  {proxy}  ({ms:.0f} ms)")



# ── Main rotation loop ─────────────────────────────────────────────────────────

def run_loop() -> None:
    # Load active proxies from file if they exist
    with _s.lock:
        _s.active = _load_active_proxies()

    # Start background health monitor
    threading.Thread(target=_health_thread, daemon=True, name="health-monitor").start()

    while True:
        # ── Re-fetch proxies when pool is empty or stale ───────────────────
        need_reload = (not _s.pool) or (time.time() - _s.last_reload > RELOAD_INTERVAL)
        if need_reload:
            candidates = fetch_all_proxies()
            if not candidates:
                print("[!] No proxies fetched. Retrying in 60s...")
                time.sleep(60)
                continue

            # Limit to first MAX_INITIAL_TEST proxies
            candidates = candidates[:MAX_INITIAL_TEST]

            with _s.lock:
                _s.pool = candidates
                _s.rotate_index = 0
                _s.last_reload = time.time()

            # ── Step 1: Test proxies SEQUENTIALLY until one works ────────────
            working_proxy, found_at_index = test_proxies_sequentially(candidates)
            if not working_proxy:
                print("[!] No working proxies in batch. Retrying in 60s...")
                time.sleep(60)
                continue

            # Apply the first working proxy immediately
            apply_proxy(working_proxy)

            # ── Step 2: Background test REMAINING proxies ──────────────────
            remaining = candidates[found_at_index + 1 :]
            if remaining:
                threading.Thread(
                    target=lambda r=remaining: verify_remaining_proxies(r),
                    daemon=True,
                    name="bg-verify-remaining",
                ).start()

        # ── Use active proxies if available, fallback to pool ─────────────
        with _s.lock:
            proxy_list = _s.active if _s.active else _s.pool

        if not proxy_list:
            print("[!] No working proxies available. Retrying in 60s...")
            time.sleep(60)
            continue

        # ── Wait for health check or planned rotation ──────────────────────
        _s.failover.clear()
        triggered = _s.failover.wait(timeout=ROTATE_INTERVAL)

        if triggered:
            # Current proxy is dead/slow — remove it and switch
            with _s.lock:
                current_proxy = _s.current
                if current_proxy and current_proxy in _s.active:
                    _s.active.remove(current_proxy)
                    _save_active_proxies(_s.active)
                    print(f"[!] Removed dead proxy: {current_proxy}")

            # Get next working proxy
            with _s.lock:
                proxy_list = _s.active if _s.active else _s.pool

            if proxy_list:
                print("[*] Switching to next proxy immediately...")
                with _s.lock:
                    idx = _s.rotate_index % len(proxy_list)
                    proxy = proxy_list[idx]
                    _s.rotate_index += 1
                apply_proxy(proxy)
            else:
                print("[!] All proxies exhausted. Fetching fresh proxies...")
                with _s.lock:
                    _s.pool = []
                    _s.last_reload = 0
                    # Retest working proxies before fetching new ones
                    print("[*] Retesting known working proxies...")
                    threading.Thread(
                        target=retest_working_proxies,
                        daemon=True,
                        name="retest-working",
                    ).start()
                time.sleep(10)  # Brief pause before retry


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Windows auto-proxy manager")
    ap.add_argument(
        "--disable",
        action="store_true",
        help="Disable the system proxy and exit",
    )
    args = ap.parse_args()

    if args.disable:
        disable_proxy()
        return

    print("=" * 70)
    print("  Windows Auto-Proxy Manager (Sequential + Background Testing)")
    print("=" * 70)
    print("  Workflow:")
    print("  1. Fetch up to 300 proxies from online sources")
    print("  2. Test proxies SEQUENTIALLY until finding one that works")
    print("  3. Apply first working proxy IMMEDIATELY")
    print("  4. Background: test remaining proxies concurrently")
    print("  5. Retest working proxies to verify they stay alive")
    print("  6. Fetch NEW proxies and repeat from step 1")
    print("=" * 70)
    print("  Health monitor: checks proxy every 30 seconds")
    print("  Auto-failover: switches to next proxy if current goes down")
    print("  Active file: active_proxies.txt")
    print("  Press Ctrl+C to stop and disable proxy")
    print("=" * 70 + "\n")

    try:
        run_loop()
    except KeyboardInterrupt:
        print("\n[*] Interrupted — disabling proxy and exiting...")
        disable_proxy()


if __name__ == "__main__":
    main()
