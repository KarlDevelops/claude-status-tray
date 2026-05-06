#!/usr/bin/env python3
"""CLI tool to fetch subscription usage for Claude, Ollama, and Codex as JSON.

Usage:
    ./usage_cli.py              # all three services (parallel)
    ./usage_cli.py claude       # only Claude
    ./usage_cli.py ollama codex # specific services
    ./usage_cli.py --pretty     # indented output
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ── Shared helpers ──────────────────────────────────────────────────

def _time_until(reset_ts):
    diff = reset_ts - time.time()
    if diff <= 0:
        return "now"
    hours = int(diff // 3600)
    mins = int((diff % 3600) // 60)
    if hours >= 24:
        days = hours // 24
        return f"{days}d {hours % 24}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _iso_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ── Claude ──────────────────────────────────────────────────────────

def _parse_ratelimit_headers(text):
    headers = {}
    for match in re.finditer(
        r'"(anthropic-ratelimit-unified-[^"]+)"\s*:\s*"([^"]*)"', text
    ):
        headers[match.group(1)] = match.group(2)
    return headers


def fetch_claude():
    try:
        env = os.environ.copy()
        env["ANTHROPIC_LOG"] = "debug"
        result = subprocess.run(
            ["claude", "-p", "hi", "--model", "haiku",
             "--max-budget-usd", "0.01", "--output-format", "stream-json", "--verbose"],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL, env=env,
        )
        output = result.stdout + "\n" + result.stderr
        h = _parse_ratelimit_headers(output)

        h5_util = h.get("anthropic-ratelimit-unified-5h-utilization")
        d7_util = h.get("anthropic-ratelimit-unified-7d-utilization")
        if not h5_util and not d7_util:
            return {"error": "No rate limit data. Check claude auth."}

        plan = None
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if creds_path.exists():
            try:
                creds = json.loads(creds_path.read_text())
                plan = creds.get("claudeAiOauth", {}).get("subscriptionType")
            except Exception:
                pass

        h5_reset = int(h["anthropic-ratelimit-unified-5h-reset"]) if h.get("anthropic-ratelimit-unified-5h-reset") else None
        d7_reset = int(h["anthropic-ratelimit-unified-7d-reset"]) if h.get("anthropic-ratelimit-unified-7d-reset") else None

        return {
            "plan": plan,
            "primary": {
                "label": "5h",
                "utilization": float(h5_util) if h5_util else 0,
                "status": h.get("anthropic-ratelimit-unified-5h-status", ""),
                "reset_at": _iso_time(h5_reset) if h5_reset else None,
                "resets_in": _time_until(h5_reset) if h5_reset else None,
            },
            "secondary": {
                "label": "7d",
                "utilization": float(d7_util) if d7_util else 0,
                "status": h.get("anthropic-ratelimit-unified-7d-status", ""),
                "reset_at": _iso_time(d7_reset) if d7_reset else None,
                "resets_in": _time_until(d7_reset) if d7_reset else None,
            },
            "overage_status": h.get("anthropic-ratelimit-unified-overage-status", "") or None,
            "overage_reason": h.get("anthropic-ratelimit-unified-overage-disabled-reason", "") or None,
        }
    except FileNotFoundError:
        return {"error": "'claude' not found in PATH."}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout fetching Claude data."}
    except Exception as e:
        return {"error": str(e)}


# ── Ollama ──────────────────────────────────────────────────────────

OLLAMA_CDP_PORT = 9224
OLLAMA_SETTINGS_URL = "https://ollama.com/settings"


def _cdp_evaluate(ws_url, expression, timeout=10):
    import socket
    from urllib.parse import urlparse
    parsed = urlparse(ws_url)
    host, port, path = parsed.hostname, parsed.port or 80, parsed.path

    sock = socket.create_connection((host, port), timeout=timeout)
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    sock.sendall((
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)

    msg = json.dumps({"id": 1, "method": "Runtime.evaluate",
                       "params": {"expression": expression}}).encode()
    frame = bytearray([0x81])
    length = len(msg)
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(0x80 | 127)
        frame.extend(length.to_bytes(8, "big"))
    frame.extend(b"\x00\x00\x00\x00")
    frame.extend(msg)
    sock.sendall(frame)

    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) >= 2:
            pl = data[1] & 0x7F
            off = 2
            if pl == 126:
                if len(data) < 4: continue
                pl = int.from_bytes(data[2:4], "big"); off = 4
            elif pl == 127:
                if len(data) < 10: continue
                pl = int.from_bytes(data[2:10], "big"); off = 10
            if len(data) >= off + pl:
                sock.close()
                return json.loads(data[off:off + pl])
    sock.close()
    return None


def fetch_ollama():
    tab_id = None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{OLLAMA_CDP_PORT}/json/new?{OLLAMA_SETTINGS_URL}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            tab_info = json.loads(resp.read())
        tab_id = tab_info["id"]
        ws_url = tab_info["webSocketDebuggerUrl"]

        for _ in range(20):
            time.sleep(0.5)
            result = _cdp_evaluate(ws_url, "document.readyState")
            if result and result.get("result", {}).get("result", {}).get("value") == "complete":
                break

        result = _cdp_evaluate(ws_url, """
            (function() {
                var text = document.body.innerText;
                var s = text.match(/Session usage\\s*([\\d.]+)% used\\s*Resets in ([^\\n]+)/);
                var w = text.match(/Weekly usage\\s*([\\d.]+)% used\\s*Resets in ([^\\n]+)/);
                var p = text.match(/Cloud Usage\\s*(\\w+)/);
                return JSON.stringify({
                    session_pct: s ? parseFloat(s[1]) : null,
                    session_reset: s ? s[2].trim() : null,
                    weekly_pct: w ? parseFloat(w[1]) : null,
                    weekly_reset: w ? w[2].trim() : null,
                    plan: p ? p[1] : null
                });
            })()
        """)
        if not result:
            return {"error": "Failed to read Ollama page data."}
        value = result.get("result", {}).get("result", {}).get("value")
        if not value:
            return {"error": "No data from Ollama settings page."}
        data = json.loads(value)
        if data.get("session_pct") is None and data.get("weekly_pct") is None:
            return {"error": "Could not parse Ollama usage. Not logged in?"}

        return {
            "plan": data.get("plan"),
            "primary": {
                "label": "session",
                "utilization": (data.get("session_pct", 0) or 0) / 100.0,
                "resets_in": data.get("session_reset"),
            },
            "secondary": {
                "label": "weekly",
                "utilization": (data.get("weekly_pct", 0) or 0) / 100.0,
                "resets_in": data.get("weekly_reset"),
            },
        }
    except Exception as e:
        return {"error": f"Ollama fetch error: {e}"}
    finally:
        if tab_id:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{OLLAMA_CDP_PORT}/json/close/{tab_id}", timeout=5)
            except Exception:
                pass


# ── Codex ───────────────────────────────────────────────────────────

_CODEX_BIN_CACHE = None


def _codex_candidate_paths():
    home = os.path.expanduser("~")
    patterns = [
        ".nvm/versions/node/*/bin/codex",
        ".fnm/node-versions/*/installation/bin/codex",
        ".local/share/fnm/node-versions/*/installation/bin/codex",
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(sorted(glob.glob(os.path.join(home, pattern)), reverse=True))
    candidates.extend([
        os.path.join(home, ".volta/bin/codex"),
        os.path.join(home, ".asdf/shims/codex"),
        os.path.join(home, ".local/bin/codex"),
        os.path.join(home, ".npm-global/bin/codex"),
        os.path.join(home, "bin/codex"),
        "/usr/local/bin/codex",
    ])
    return candidates


def _resolve_codex_binary():
    """Locate the codex CLI even when launched without nvm in PATH."""
    global _CODEX_BIN_CACHE
    if _CODEX_BIN_CACHE:
        if os.path.isfile(_CODEX_BIN_CACHE) and os.access(_CODEX_BIN_CACHE, os.X_OK):
            return _CODEX_BIN_CACHE
        _CODEX_BIN_CACHE = None

    found = shutil.which("codex")
    if not found:
        for path in _codex_candidate_paths():
            if os.path.isfile(path) and os.access(path, os.X_OK):
                found = path
                break

    if found:
        _CODEX_BIN_CACHE = found
    return found


def _codex_path_dirs(codex_bin):
    dirs = []

    def add_dir(path):
        if path and path not in dirs:
            dirs.append(path)

    codex_path = Path(codex_bin)
    add_dir(str(codex_path.parent))

    paths = [codex_path]
    try:
        paths.append(codex_path.resolve())
    except OSError:
        pass

    for path in paths:
        for parent in path.parents:
            node_bin = parent / "bin" / "node"
            if node_bin.is_file() and os.access(node_bin, os.X_OK):
                add_dir(str(node_bin.parent))
                break

    for candidate in _codex_candidate_paths():
        node_bin = Path(candidate).with_name("node")
        if node_bin.is_file() and os.access(node_bin, os.X_OK):
            add_dir(str(node_bin.parent))

    return dirs


def _prepend_path_dirs(env, dirs):
    current = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    prefix = [p for p in dirs if p and p not in current]
    if prefix:
        env["PATH"] = os.pathsep.join(prefix + current)


def _parse_codex_rate_limits_legacy(text):
    json_match = re.search(r'(\{"type":"codex\.rate_limits".*?\})\s*$', text, re.MULTILINE)
    if not json_match:
        json_match = re.search(r'Received message (\{"type":"codex\.rate_limits".*?\})', text)
    if not json_match:
        return None
    data = json.loads(json_match.group(1))
    rl = data.get("rate_limits", {})
    primary = rl.get("primary", {})
    secondary = rl.get("secondary", {})
    return {
        "primary_pct": primary.get("used_percent", 0),
        "primary_window_min": primary.get("window_minutes", 300),
        "primary_reset_ts": primary.get("reset_at"),
        "secondary_pct": secondary.get("used_percent", 0),
        "secondary_window_min": secondary.get("window_minutes", 10080),
        "secondary_reset_ts": secondary.get("reset_at"),
        "plan": data.get("plan_type"),
        "allowed": rl.get("allowed", True),
        "limit_reached": rl.get("limit_reached", False),
    }


def _parse_codex_rate_limits_headers(text):
    def _find(name):
        m = re.search(rf'"{re.escape(name)}"\s*:\s*"([^"]*)"', text)
        return m.group(1) if m else None

    plan = _find("X-Codex-Plan-Type")
    primary_raw = _find("X-Codex-Primary-Used-Percent")
    if plan is None and primary_raw is None:
        return None

    def _to_int(v, default=None):
        if v is None or v == "":
            return default
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default

    limit_reached = "usage_limit_reached" in text
    return {
        "primary_pct": _to_int(primary_raw, 0),
        "primary_window_min": _to_int(_find("X-Codex-Primary-Window-Minutes"), 300),
        "primary_reset_ts": _to_int(_find("X-Codex-Primary-Reset-At")),
        "secondary_pct": _to_int(_find("X-Codex-Secondary-Used-Percent"), 0),
        "secondary_window_min": _to_int(_find("X-Codex-Secondary-Window-Minutes"), 10080),
        "secondary_reset_ts": _to_int(_find("X-Codex-Secondary-Reset-At")),
        "plan": plan,
        "allowed": not limit_reached,
        "limit_reached": limit_reached,
    }


def _window_label(minutes):
    minutes = minutes or 0
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _format_codex(parsed):
    p_reset = parsed.get("primary_reset_ts")
    s_reset = parsed.get("secondary_reset_ts")
    return {
        "plan": parsed.get("plan"),
        "allowed": parsed.get("allowed", True),
        "limit_reached": parsed.get("limit_reached", False),
        "primary": {
            "label": _window_label(parsed.get("primary_window_min", 300)),
            "utilization": (parsed.get("primary_pct") or 0) / 100.0,
            "reset_at": _iso_time(p_reset) if p_reset else None,
            "resets_in": _time_until(p_reset) if p_reset else None,
        },
        "secondary": {
            "label": _window_label(parsed.get("secondary_window_min", 10080)),
            "utilization": (parsed.get("secondary_pct") or 0) / 100.0,
            "reset_at": _iso_time(s_reset) if s_reset else None,
            "resets_in": _time_until(s_reset) if s_reset else None,
        },
    }


def fetch_codex():
    codex_bin = _resolve_codex_binary()
    if not codex_bin:
        return {"error": "'codex' not found in PATH."}
    try:
        env = os.environ.copy()
        env["RUST_LOG"] = "trace"
        _prepend_path_dirs(env, _codex_path_dirs(codex_bin))
        result = subprocess.run(
            [codex_bin, "exec", "say ok"],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL, env=env,
        )
        output = result.stdout + "\n" + result.stderr
        parsed = _parse_codex_rate_limits_legacy(output)
        if parsed is None:
            parsed = _parse_codex_rate_limits_headers(output)
        if parsed is None:
            return {"error": "No rate limit data from Codex CLI."}
        return _format_codex(parsed)
    except FileNotFoundError:
        return {"error": "'codex' not found in PATH."}
    except subprocess.TimeoutExpired:
        return {"error": "Timeout fetching Codex data."}
    except Exception as e:
        return {"error": f"Codex fetch error: {e}"}


# ── Main ────────────────────────────────────────────────────────────

SERVICES = {
    "claude": fetch_claude,
    "ollama": fetch_ollama,
    "codex": fetch_codex,
}


def main():
    parser = argparse.ArgumentParser(
        description="Fetch subscription usage for Claude, Ollama, and Codex.")
    parser.add_argument(
        "services", nargs="*", metavar="SERVICE",
        help="Services to query (default: all). Choices: claude, ollama, codex")
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print JSON output")
    args = parser.parse_args()

    services = args.services or list(SERVICES.keys())
    for s in services:
        if s not in SERVICES:
            parser.error(f"unknown service '{s}'. Choose from: {', '.join(SERVICES)}")
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(SERVICES[s]): s for s in services}
        for future in as_completed(futures):
            svc = futures[future]
            try:
                results[svc] = future.result()
            except Exception as e:
                results[svc] = {"error": str(e)}

    output = {"timestamp": datetime.now(timezone.utc).isoformat(), "services": results}
    indent = 2 if args.pretty else None
    json.dump(output, sys.stdout, indent=indent, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
