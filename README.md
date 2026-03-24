# Windows_Proxy_Manager

A Python-based tool that automatically fetches, tests, applies, and rotates free HTTP proxies on Windows.
It sources proxies from public GitHub repositories, tests them for speed and availability, and applies the best-working proxy to your Windows system settings with automatic failover and rotation.

---

## Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Project Structure](#project-structure)
- [How It Works — End to End](#how-it-works--end-to-end)
  - [Step 1: Fetching Proxies](#step-1-fetching-proxies)
  - [Step 2: Finding the First Working Proxy](#step-2-finding-the-first-working-proxy)
  - [Step 3: Applying Proxy to Windows](#step-3-applying-proxy-to-windows)
  - [Step 4: Building a Proxy Pool in the Background](#step-4-building-a-proxy-pool-in-the-background)
  - [Step 5: Health Monitoring](#step-5-health-monitoring)
  - [Step 6: Rotation and Failover](#step-6-rotation-and-failover)
  - [Step 7: Periodic Re-fetch](#step-7-periodic-re-fetch)
- [Configuration Reference](#configuration-reference)
- [Output Files](#output-files)
- [Windows Proxy Coverage](#windows-proxy-coverage)
- [Usage](#usage)

---

## Overview

```
Fetch proxies → Test sequentially → Apply first working proxy → Background-test the rest
     ↑                                                                        |
     |                                                                        ↓
Re-fetch every 1 hour  ←  Health monitor / Auto-failover  ←  Build active pool
```

The tool keeps your Windows system proxy **always active** by:

1. Pulling proxies from 5 public sources
2. Finding one that works as fast as possible
3. Applying it to Windows **immediately**
4. Building a pool of verified working proxies in the background
5. Auto-switching if the current proxy dies or becomes too slow

---

## Requirements

- Python 3.10+
- Windows OS
- `requests` library

```bash
pip install requests
```

- **Administrator privileges** recommended (required for WinHTTP / `netsh` coverage)

---

## Project Structure

```
Proxy Setup/
├── Proxy.py              # Main program — testing, rotation, Windows integration
├── fetcher.py            # Downloads proxy lists from public GitHub sources
├── active_proxies.txt    # Live list of verified working proxies (auto-updated)
└── working_proxies.txt   # Legacy compatibility file
```

---

## How It Works — End to End

### Step 1: Fetching Proxies

Proxies are downloaded from 5 well-known public GitHub repositories that update their lists frequently:

| Source | Repository |
|--------|-----------|
| TheSpeedX | `TheSpeedX/PROXY-List` |
| clarketm | `clarketm/proxy-list` |
| monosans | `monosans/proxy-list` |
| ShiftyTR | `ShiftyTR/Proxy-List` |
| jetkai | `jetkai/proxy-list` |

All 5 sources are downloaded **at the same time** (concurrently), so the total fetch time equals only the slowest single source. Each download has a 15-second timeout.

Once downloaded, invalid entries, blank lines, comments, and malformed addresses are all discarded. Only valid `IP:PORT` addresses are kept. The result is a single deduplicated list with no duplicates across sources.

Only the first **300 proxies** from this list are used per cycle to keep testing time manageable.

---

### Step 2: Finding the First Working Proxy

Each proxy is tested **one by one** in order. Testing a proxy means sending a real HTTP request through it to an external website and checking if a valid response comes back within 6 seconds.

- If the proxy responds correctly → it is marked as working and the test stops immediately
- If the proxy times out or fails → move to the next one

This approach is intentional: rather than waiting for all 300 proxies to be tested before getting any result, the program stops as soon as it finds **one working proxy** and applies it to Windows right away within seconds.

---

### Step 3: Applying Proxy to Windows

Once a working proxy is found, it is applied to **two** Windows proxy systems at the same time:

#### WinInet (Registry)

The proxy is written directly into the Windows Registry under Internet Settings. Windows is then notified of the change instantly. there is no need to reboot or restarting browser.

**Coverage:** Edge, Chrome, Internet Explorer, and most desktop apps.

#### WinHTTP

The proxy is applied via the `netsh winhttp` system command.

**Coverage:** Windows background services, system update agents, and apps that bypass WinInet.

> **Note:** WinHTTP requires Administrator privileges. Without it, most apps (browsers, etc.) still get the proxy via WinInet.

The new proxy is written **before** the old one is removed so there is zero gap in proxy coverage during a switch.

---

### Step 4: Building a Proxy Pool in the Background

While the first working proxy is already active and in use, all remaining proxies from the list are tested **simultaneously** in the background using up to 50 parallel threads.

- Working proxies are collected and sorted fastest-first by response time
- They are merged with any previously known working proxies
- The combined list is saved to `active_proxies.txt` and kept in memory

This builds a **ready pool** of verified proxies that the rotation logic draws from — so future switches are instant with no re-testing needed.

---

### Step 5: Health Monitoring

A background process runs continuously and checks the **currently active proxy every 30 seconds** by sending a real test request through it.

```
Every 30 seconds:

  No response at all  →  Switch proxy immediately
  Response too slow   →  Switch proxy immediately (threshold: 3000ms)
  Response is fine    →  Continue, log OK
```

When a problem is detected, the main program is woken up **instantly** to switch. It does not wait for the next scheduled rotation.

---

### Step 6: Rotation and Failover

The proxy is switched in two situations:

- **Planned rotation** → every 5 minutes, the next proxy from the active pool is applied automatically (round-robin)
- **Emergency failover** → if the health monitor detects a dead or slow proxy, the current proxy is removed from the pool and the next available one is applied immediately

The program always prefers the **verified active pool** first. If that pool is empty, it falls back to the broader candidate list. If everything is exhausted, it re-tests the known list and then fetches fresh proxies.

---

### Step 7: Periodic Re-fetch

Every **1 hour**, the entire process restarts from Step 1 → fresh proxies are downloaded from all 5 sources. This keeps the pool up to date since public proxy lists change frequently and proxies go offline over time.

---

## Configuration Reference

All tuneable constants are at the top of `Proxy.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `CHECK_TIMEOUT` | `6` seconds | Per-proxy test timeout |
| `MAX_LATENCY_MS` | `3000` ms | Drop proxy if slower than this |
| `MAX_WORKERS` | `50` | Concurrent threads for bulk testing |
| `ROTATE_INTERVAL` | `300` seconds (5 min) | Planned rotation interval |
| `RELOAD_INTERVAL` | `3600` seconds (1 hr) | Re-fetch from sources interval |
| `HEALTH_INTERVAL` | `30` seconds | Health-check polling interval |
| `MAX_INITIAL_TEST` | `300` | Max proxies to test per fetch cycle |
| `FETCH_TIMEOUT` | `15` seconds | Timeout for downloading proxy lists |

---

## Output Files

| File | Description |
|------|-------------|
| `active_proxies.txt` | Live list of verified working proxies. Updated automatically as proxies are found or die. Loaded on startup so the program resumes from where it left off. |
| `working_proxies.txt` | Legacy file kept for backwards compatibility. |

---

## Windows Proxy Coverage

| Proxy System | Coverage | Admin Required |
|---|---|---|
| WinInet (Registry) | Edge, Chrome, IE, most desktop apps | No |
| WinHTTP | Windows services, background system apps | Yes |

---

## Usage

**Start the proxy manager:**

```bash
python Proxy.py
```

**Disable system proxy and exit:**

```bash
python Proxy.py --disable
```

**Recommended: run as Administrator** for full system coverage:

```bash
# Right-click terminal → "Run as Administrator", then:
python Proxy.py
```

**Stop:** Press `Ctrl+C` → the proxy is automatically disabled before exit.

---

## Startup Flow Summary

```
python Proxy.py
       │
       ├─ Load active_proxies.txt (resume from last session if it exists)
       ├─ Start health monitor in background (checks every 30s)
       │
       └─ Main Loop
              │
              ├─ Download proxies from 5 sources simultaneously
              │        └─ Clean, deduplicate, cap at 300
              │
              ├─ Test proxies one by one until first working one is found
              │        └─ Apply to Windows immediately (Registry + WinHTTP)
              │
              ├─ Test all remaining proxies in background (50 at a time)
              │        └─ Build active pool, save to active_proxies.txt
              │
              └─ Wait up to 5 minutes (or until health monitor triggers)
                       │
                       ├─ [failover]  remove dead proxy → apply next immediately
                       └─ [5 min]     planned rotation → round-robin to next proxy
```
