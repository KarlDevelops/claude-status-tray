#!/usr/bin/env python3
"""Claude/Ollama Usage Tray Icon - Shows usage limits in the system tray."""

import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, Gdk, GLib, AyatanaAppIndicator3


def _install_css():
    css = Gtk.CssProvider()
    css.load_from_data(b"""
        #usage-menu menuitem {
            padding: 1px 4px;
            min-height: 0;
        }
    """)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


# ── Icon ──────────────────────────────────────────────────────────────

ICON_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<svg width="64" height="64" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">
  <circle cx="32" cy="32" r="30" fill="{color}"/>
  <text x="32" y="{y}" text-anchor="middle" font-size="{size}" font-family="sans-serif"
        font-weight="900" fill="white" stroke="white" stroke-width="{stroke}">{letter}</text>
</svg>"""

ICON_COLORS = {
    "green": "#22c55e",
    "orange": "#f59e0b",
    "red": "#ef4444",
    "default": "#D97706",
}

ICON_DIR = "/tmp"


def _icon_path(color, alert=False, prefix="claude"):
    suffix = "-alert" if alert else ""
    return f"{ICON_DIR}/{prefix}-tray-{color}{suffix}.svg"


def create_icons():
    for prefix, base_letter in (("claude", "C"), ("ollama", "O"), ("codex", "X")):
        for name, hex_color in ICON_COLORS.items():
            for alert in (False, True):
                path = _icon_path(name, alert, prefix)
                if alert:
                    letter, size, y, stroke = "!", 48, 48, 2
                else:
                    letter, size, y, stroke = base_letter, 36, 44, 0
                with open(path, "w") as f:
                    f.write(ICON_TEMPLATE.format(
                        color=hex_color, letter=letter,
                        size=size, y=y, stroke=stroke,
                    ))


# ── Data fetching ────────────────────────────────────────────────────

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path.home() / ".config" / "claude-status-tray" / "config.json"


def _load_config_file():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config_file(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def _time_until(reset_ts, show_days=False):
    diff = reset_ts - time.time()
    if diff <= 0:
        return "now"
    hours = int(diff // 3600)
    mins = int((diff % 3600) // 60)
    if show_days and hours >= 24:
        days = hours // 24
        return f"{days}d {hours % 24}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _local_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M")


def _parse_ratelimit_headers(text):
    headers = {}
    for match in re.finditer(
        r'"(anthropic-ratelimit-unified-[^"]+)"\s*:\s*"([^"]*)"', text
    ):
        headers[match.group(1)] = match.group(2)
    return headers


def fetch_usage_data():
    """Fetch live rate limit data. Returns a dict or error string."""
    try:
        env = os.environ.copy()
        env["ANTHROPIC_LOG"] = "debug"

        result = subprocess.run(
            [
                "claude", "-p", "hi",
                "--model", "haiku",
                "--max-budget-usd", "0.01",
                "--output-format", "stream-json",
                "--verbose",
            ],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL, env=env,
        )

        output = result.stdout + "\n" + result.stderr
        h = _parse_ratelimit_headers(output)

        cost = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line.strip())
                if event.get("type") == "result":
                    cost = event.get("total_cost_usd", 0)
            except (json.JSONDecodeError, ValueError):
                continue

        h5_util = h.get("anthropic-ratelimit-unified-5h-utilization")
        d7_util = h.get("anthropic-ratelimit-unified-7d-utilization")
        if not h5_util and not d7_util:
            return "No rate limit data. Check claude auth."

        plan = "?"
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if creds_path.exists():
            try:
                creds = json.loads(creds_path.read_text())
                plan = creds.get("claudeAiOauth", {}).get("subscriptionType", "?")
            except Exception:
                pass

        return {
            "h5_util": float(h5_util) if h5_util else 0,
            "h5_status": h.get("anthropic-ratelimit-unified-5h-status", ""),
            "h5_reset": int(h["anthropic-ratelimit-unified-5h-reset"]) if h.get("anthropic-ratelimit-unified-5h-reset") else None,
            "d7_util": float(d7_util) if d7_util else 0,
            "d7_status": h.get("anthropic-ratelimit-unified-7d-status", ""),
            "d7_reset": int(h["anthropic-ratelimit-unified-7d-reset"]) if h.get("anthropic-ratelimit-unified-7d-reset") else None,
            "overage_status": h.get("anthropic-ratelimit-unified-overage-status", ""),
            "overage_reason": h.get("anthropic-ratelimit-unified-overage-disabled-reason", ""),
            "plan": plan,
            "cost": cost,
        }

    except FileNotFoundError:
        return "'claude' not found in PATH."
    except subprocess.TimeoutExpired:
        return "Timeout fetching data."
    except Exception as e:
        return str(e)


# ── Ollama usage (via Snap Chromium CDP) ────────────────────────────

OLLAMA_CDP_PORT = 9224
OLLAMA_SETTINGS_URL = "https://ollama.com/settings"


def _cdp_evaluate(ws_url, expression, timeout=10):
    """Evaluate JS in a CDP tab using a raw WebSocket (no async deps)."""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(ws_url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path

    sock = socket.create_connection((host, port), timeout=timeout)
    # WebSocket handshake
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(handshake.encode())

    # Read handshake response
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)

    # Send CDP message
    msg = json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": expression},
    }).encode()

    # Build WebSocket frame (text, masked with zero mask for local)
    frame = bytearray()
    frame.append(0x81)  # FIN + text
    length = len(msg)
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(0x80 | 127)
        frame.extend(length.to_bytes(8, "big"))
    frame.extend(b"\x00\x00\x00\x00")  # zero mask key
    frame.extend(msg)
    sock.sendall(frame)

    # Read response frames
    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) >= 2:
            payload_len = data[1] & 0x7F
            offset = 2
            if payload_len == 126:
                if len(data) < 4:
                    continue
                payload_len = int.from_bytes(data[2:4], "big")
                offset = 4
            elif payload_len == 127:
                if len(data) < 10:
                    continue
                payload_len = int.from_bytes(data[2:10], "big")
                offset = 10
            if len(data) >= offset + payload_len:
                payload = data[offset:offset + payload_len]
                sock.close()
                return json.loads(payload)

    sock.close()
    return None


def fetch_ollama_usage():
    """Open ollama.com/settings in Snap Chromium, scrape usage, close tab."""
    tab_id = None
    try:
        # Open a new tab
        req = urllib.request.Request(
            f"http://127.0.0.1:{OLLAMA_CDP_PORT}/json/new?{OLLAMA_SETTINGS_URL}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            tab_info = json.loads(resp.read())
        tab_id = tab_info["id"]
        ws_url = tab_info["webSocketDebuggerUrl"]

        # Poll until the page has loaded (up to 10s)
        for _ in range(20):
            time.sleep(0.5)
            result = _cdp_evaluate(ws_url, "document.readyState")
            if result:
                state = result.get("result", {}).get("result", {}).get("value")
                if state == "complete":
                    break

        # Extract usage data
        result = _cdp_evaluate(ws_url, """
            (function() {
                var text = document.body.innerText;
                var sessionMatch = text.match(/Session usage\\s*([\\d.]+)% used\\s*Resets in ([^\\n]+)/);
                var weeklyMatch = text.match(/Weekly usage\\s*([\\d.]+)% used\\s*Resets in ([^\\n]+)/);
                var planMatch = text.match(/Cloud Usage\\s*(\\w+)/);
                return JSON.stringify({
                    session_pct: sessionMatch ? parseFloat(sessionMatch[1]) : null,
                    session_reset: sessionMatch ? sessionMatch[2].trim() : null,
                    weekly_pct: weeklyMatch ? parseFloat(weeklyMatch[1]) : null,
                    weekly_reset: weeklyMatch ? weeklyMatch[2].trim() : null,
                    plan: planMatch ? planMatch[1] : "Unknown"
                });
            })()
        """)

        if not result:
            return "Failed to read Ollama page data."

        value = result.get("result", {}).get("result", {}).get("value")
        if not value:
            return "No data from Ollama settings page."

        data = json.loads(value)
        if data.get("session_pct") is None and data.get("weekly_pct") is None:
            return "Could not parse Ollama usage. Make sure you're logged in at ollama.com/settings."

        return {
            "session_pct": data.get("session_pct", 0),
            "session_reset": data.get("session_reset", ""),
            "weekly_pct": data.get("weekly_pct", 0),
            "weekly_reset": data.get("weekly_reset", ""),
            "plan": data.get("plan", "Unknown"),
        }

    except Exception as e:
        return f"Ollama fetch error: {e}"

    finally:
        # Always close the tab
        if tab_id:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{OLLAMA_CDP_PORT}/json/close/{tab_id}",
                    timeout=5,
                )
            except Exception:
                pass


# ── Codex usage (via codex CLI) ─────────────────────────────────────

_CODEX_BIN_CACHE = None
_CODEX_USAGE_PROMPT = "say ok"
_CODEX_SESSION_CONFIG_KEY = "codex_usage_session_id"
_CODEX_LATENCY_LOG_PATH = CONFIG_PATH.parent / "codex_latency.jsonl"
_CODEX_LATENCY_WINDOWS_HOURS = (6, 12, 18)
_CODEX_SLOW_STREAK_THRESHOLD = 3
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


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
    """Locate the codex CLI even when the tray is launched without nvm in PATH."""
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
    """PATH entries needed by codex and its node runtime in desktop autostart."""
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


def _get_codex_usage_session_id():
    session_id = _load_config_file().get(_CODEX_SESSION_CONFIG_KEY)
    if isinstance(session_id, str) and re.fullmatch(_UUID_RE, session_id):
        return session_id
    return None


def _set_codex_usage_session_id(session_id):
    if not session_id or not re.fullmatch(_UUID_RE, session_id):
        return
    cfg = _load_config_file()
    if cfg.get(_CODEX_SESSION_CONFIG_KEY) == session_id:
        return
    cfg[_CODEX_SESSION_CONFIG_KEY] = session_id
    _save_config_file(cfg)


def _clear_codex_usage_session_id(session_id):
    cfg = _load_config_file()
    if cfg.get(_CODEX_SESSION_CONFIG_KEY) == session_id:
        cfg.pop(_CODEX_SESSION_CONFIG_KEY, None)
        _save_config_file(cfg)


def _extract_codex_session_id(text):
    patterns = [
        rf"\bconversation\.id=({_UUID_RE})",
        rf"\bthread_id=({_UUID_RE})",
        rf"thread ID: Some\(ThreadId \{{ uuid: ({_UUID_RE}) \}}\)",
        rf'"thread_id"\s*:\s*"({_UUID_RE})"',
        rf"rollout-[^\s\"]*-({_UUID_RE})\.jsonl",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).lower()
    return None


def _codex_resume_failed(text):
    lower = text.lower()
    return (
        "no recorded session" in lower
        or "not found" in lower and "session" in lower
        or "failed to resume" in lower
        or "thread/resume" in lower and "error" in lower
    )


def _run_codex_usage_probe(codex_bin, env, session_id=None):
    if session_id:
        args = [codex_bin, "exec", "resume", session_id, _CODEX_USAGE_PROMPT]
    else:
        args = [codex_bin, "exec", _CODEX_USAGE_PROMPT]
    started_at = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    attempt = {
        "started_at": started_at.isoformat(),
        "resumed": bool(session_id),
        "session_id": session_id,
        "timeout": False,
        "returncode": None,
    }
    try:
        result = subprocess.run(
            args,
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL, env=env, cwd=str(APP_DIR),
        )
        output = result.stdout + "\n" + result.stderr
        attempt["returncode"] = result.returncode
    except subprocess.TimeoutExpired as exc:
        result = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        output = stdout + "\n" + stderr
        attempt["timeout"] = True
    attempt["duration_seconds"] = round(time.monotonic() - started_mono, 3)
    return result, output, attempt


def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _round_seconds(value):
    return round(value, 3) if _is_number(value) else None


def _codex_latency_stats(values):
    values = sorted(v for v in values if _is_number(v))
    if not values:
        return {
            "count": 0,
            "avg_seconds": None,
            "median_seconds": None,
            "p90_seconds": None,
        }
    mid = len(values) // 2
    if len(values) % 2:
        median = values[mid]
    else:
        median = (values[mid - 1] + values[mid]) / 2
    p90_idx = max(0, min(len(values) - 1, (len(values) * 90 + 99) // 100 - 1))
    return {
        "count": len(values),
        "avg_seconds": _round_seconds(sum(values) / len(values)),
        "median_seconds": _round_seconds(median),
        "p90_seconds": _round_seconds(values[p90_idx]),
    }


def _load_codex_latency_records():
    records = []
    try:
        with _CODEX_LATENCY_LOG_PATH.open() as f:
            for line in f:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_iso_datetime(record.get("timestamp") or record.get("started_at"))
                if ts is None:
                    continue
                record["_ts"] = ts
                records.append(record)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return records


def _summarize_codex_latency(records, now=None):
    now = now or datetime.now(timezone.utc)
    windows = {}
    for hours in _CODEX_LATENCY_WINDOWS_HOURS:
        cutoff = now - timedelta(hours=hours)
        subset = [r for r in records if r.get("_ts") and r["_ts"] >= cutoff]
        successful = [
            r.get("total_duration_seconds") for r in subset
            if r.get("success") and not r.get("timeout")
        ]
        answers = [
            r.get("session_answer_seconds") for r in subset
            if _is_number(r.get("session_answer_seconds"))
        ]
        turns = [
            r.get("session_turn_seconds") for r in subset
            if _is_number(r.get("session_turn_seconds"))
        ]
        windows[f"{hours}h"] = {
            "records": len(subset),
            "timeouts": sum(1 for r in subset if r.get("timeout")),
            "total": _codex_latency_stats(successful),
            "session_answer": _codex_latency_stats(answers),
            "session_turn": _codex_latency_stats(turns),
        }
    latest = max(records, key=lambda r: r["_ts"], default=None)
    latest_summary = None
    if latest:
        latest_summary = {
            "timestamp": latest.get("timestamp"),
            "total_duration_seconds": latest.get("total_duration_seconds"),
            "session_answer_seconds": latest.get("session_answer_seconds"),
            "session_turn_seconds": latest.get("session_turn_seconds"),
            "timeout": latest.get("timeout", False),
            "warning": latest.get("warning", False),
        }
    return {"windows": windows, "latest": latest_summary}


def _find_codex_session_file(session_id):
    if not session_id:
        return None
    base = Path.home() / ".codex" / "sessions"
    try:
        matches = list(base.rglob(f"*{session_id}.jsonl"))
    except Exception:
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _latest_codex_session_latency(session_id, since=None):
    session_file = _find_codex_session_file(session_id)
    if session_file is None:
        return None

    latest = None
    current = None
    try:
        with session_file.open() as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_iso_datetime(event.get("timestamp"))
                if ts is None:
                    continue
                payload = event.get("payload") or {}
                payload_type = payload.get("type")
                if event.get("type") == "event_msg" and payload_type == "task_started":
                    if current and not current.get("ignore"):
                        latest = current
                    current = {"task_started": ts}
                    continue
                if current is None or event.get("type") != "event_msg":
                    continue
                if payload_type == "user_message":
                    if payload.get("message") == _CODEX_USAGE_PROMPT:
                        current["user_message"] = ts
                    else:
                        current["ignore"] = True
                elif payload_type == "agent_message":
                    current.setdefault("agent_message", ts)
                elif payload_type == "task_complete":
                    current.setdefault("task_complete", ts)
        if current and not current.get("ignore"):
            latest = current
    except Exception:
        return None

    if not latest or "user_message" not in latest or "agent_message" not in latest:
        return None
    if since and latest["user_message"] < since - timedelta(seconds=10):
        return None

    task_end = latest.get("task_complete") or latest["agent_message"]
    return {
        "session_file": str(session_file),
        "session_user_at": latest["user_message"].isoformat(),
        "session_agent_at": latest["agent_message"].isoformat(),
        "session_answer_seconds": _round_seconds(
            (latest["agent_message"] - latest["user_message"]).total_seconds()
        ),
        "session_turn_seconds": _round_seconds(
            (task_end - latest.get("task_started", latest["user_message"])).total_seconds()
        ),
    }


def _evaluate_codex_latency(record, previous_records, previous_summary):
    reasons = []
    duration = record.get("total_duration_seconds")
    if record.get("timeout"):
        reasons.append("timeout")
    elif _is_number(duration):
        total_18h = previous_summary["windows"]["18h"]["total"]
        total_6h = previous_summary["windows"]["6h"]["total"]
        p90_18h = total_18h.get("p90_seconds")
        median_6h = total_6h.get("median_seconds")
        if total_18h.get("count", 0) >= 5 and _is_number(p90_18h) and duration > p90_18h:
            reasons.append(f"above 18h p90 ({p90_18h:.1f}s)")
        if total_6h.get("count", 0) >= 3 and _is_number(median_6h) and duration > median_6h * 2:
            reasons.append(f"above 2x 6h median ({median_6h:.1f}s)")

    current_bad = bool(reasons)
    streak = 1 if current_bad else 0
    if current_bad:
        for previous in reversed(previous_records):
            if previous.get("warning") or previous.get("timeout"):
                streak += 1
            else:
                break
        if streak >= _CODEX_SLOW_STREAK_THRESHOLD:
            reasons.append(f"{streak} slow checks in a row")

    return {
        "warning": bool(reasons),
        "warning_reasons": reasons,
        "slow_streak": streak if current_bad else 0,
    }


def _append_codex_latency_record(record):
    try:
        _CODEX_LATENCY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _CODEX_LATENCY_LOG_PATH.open("a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def _record_codex_latency(started_at, total_duration, attempts, session_id, output, success, error=None):
    session_latency = _latest_codex_session_latency(session_id, started_at)
    previous_records = _load_codex_latency_records()
    previous_summary = _summarize_codex_latency(previous_records)
    record = {
        "timestamp": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "success": bool(success),
        "error": error,
        "timeout": any(a.get("timeout") for a in attempts),
        "resume_retried": len(attempts) > 1,
        "attempts": attempts,
        "total_duration_seconds": _round_seconds(total_duration),
    }
    if session_latency:
        record.update(session_latency)

    record.update(_evaluate_codex_latency(record, previous_records, previous_summary))
    clean_record = {k: v for k, v in record.items() if not k.startswith("_")}
    _append_codex_latency_record(clean_record)

    current_record = dict(clean_record)
    current_record["_ts"] = started_at
    summary = _summarize_codex_latency(previous_records + [current_record])
    return {
        "current": {
            "timestamp": clean_record.get("timestamp"),
            "total_duration_seconds": clean_record.get("total_duration_seconds"),
            "session_answer_seconds": clean_record.get("session_answer_seconds"),
            "session_turn_seconds": clean_record.get("session_turn_seconds"),
            "success": clean_record.get("success"),
            "timeout": clean_record.get("timeout"),
            "warning": clean_record.get("warning"),
            "warning_reasons": clean_record.get("warning_reasons"),
            "slow_streak": clean_record.get("slow_streak"),
        },
        "windows": summary["windows"],
        "latest": summary["latest"],
        "log_path": str(_CODEX_LATENCY_LOG_PATH),
    }


def _parse_codex_rate_limits_legacy(text):
    """Old codex format (<=0.124): a {"type":"codex.rate_limits", ...} websocket message."""
    json_match = re.search(r'(\{"type":"codex\.rate_limits".*?\})\s*$', text, re.MULTILINE)
    if not json_match:
        json_match = re.search(r'Received message (\{"type":"codex\.rate_limits".*?\})', text)
    if not json_match:
        return None
    try:
        data = json.loads(json_match.group(1))
    except json.JSONDecodeError:
        return None
    rl = data.get("rate_limits", {})
    primary = rl.get("primary", {})
    secondary = rl.get("secondary", {})
    return {
        "primary_pct": primary.get("used_percent", 0),
        "primary_window_min": primary.get("window_minutes", 300),
        "primary_reset_ts": primary.get("reset_at"),
        "primary_reset_secs": primary.get("reset_after_seconds"),
        "secondary_pct": secondary.get("used_percent", 0),
        "secondary_window_min": secondary.get("window_minutes", 10080),
        "secondary_reset_ts": secondary.get("reset_at"),
        "secondary_reset_secs": secondary.get("reset_after_seconds"),
        "plan": data.get("plan_type", "unknown"),
        "allowed": rl.get("allowed", True),
        "limit_reached": rl.get("limit_reached", False),
    }


def _parse_codex_rate_limits_headers(text):
    """New codex format (>=0.125): X-Codex-* fields embedded in websocket event JSON."""
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
        "primary_reset_secs": _to_int(_find("X-Codex-Primary-Reset-After-Seconds")),
        "secondary_pct": _to_int(_find("X-Codex-Secondary-Used-Percent"), 0),
        "secondary_window_min": _to_int(_find("X-Codex-Secondary-Window-Minutes"), 10080),
        "secondary_reset_ts": _to_int(_find("X-Codex-Secondary-Reset-At")),
        "secondary_reset_secs": _to_int(_find("X-Codex-Secondary-Reset-After-Seconds")),
        "plan": plan or "unknown",
        "allowed": not limit_reached,
        "limit_reached": limit_reached,
    }


def fetch_codex_usage():
    """Fetch Codex usage by reusing one tray-owned codex exec session."""
    codex_bin = _resolve_codex_binary()
    if not codex_bin:
        return "'codex' not found in PATH."
    started_at = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    attempts = []
    try:
        env = os.environ.copy()
        env["RUST_LOG"] = "trace"
        _prepend_path_dirs(env, _codex_path_dirs(codex_bin))

        session_id = _get_codex_usage_session_id()
        result, output, attempt = _run_codex_usage_probe(codex_bin, env, session_id)
        attempts.append(attempt)
        if attempt.get("timeout"):
            _record_codex_latency(
                started_at, time.monotonic() - started_mono, attempts,
                session_id, output, False, "timeout",
            )
            return "Timeout fetching Codex data."
        if session_id and result and result.returncode != 0 and _codex_resume_failed(output):
            _clear_codex_usage_session_id(session_id)
            session_id = None
            result, output, attempt = _run_codex_usage_probe(codex_bin, env)
            attempts.append(attempt)
            if attempt.get("timeout"):
                _record_codex_latency(
                    started_at, time.monotonic() - started_mono, attempts,
                    session_id, output, False, "timeout",
                )
                return "Timeout fetching Codex data."

        parsed = _parse_codex_rate_limits_legacy(output)
        if parsed is None:
            parsed = _parse_codex_rate_limits_headers(output)
        new_session_id = _extract_codex_session_id(output)
        if parsed is not None and new_session_id:
            _set_codex_usage_session_id(new_session_id)
        effective_session_id = new_session_id or session_id
        latency = _record_codex_latency(
            started_at, time.monotonic() - started_mono, attempts,
            effective_session_id, output, parsed is not None,
            None if parsed is not None else "no_rate_limit_data",
        )
        if parsed is None:
            return "No rate limit data from Codex CLI."
        parsed["latency"] = latency
        return parsed

    except FileNotFoundError:
        return "'codex' not found in PATH."
    except Exception as e:
        return f"Codex fetch error: {e}"


# ── Status feed ──────────────────────────────────────────────────────

ATOM_NS = "{http://www.w3.org/2005/Atom}"
STATUS_URL = "https://status.claude.com/history.atom"


def fetch_incidents():
    """Fetch open incidents from status.claude.com. Returns a list of dicts."""
    try:
        req = urllib.request.Request(STATUS_URL, headers={"User-Agent": "claude-tray"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)

        incidents = []
        for entry in root.findall(f"{ATOM_NS}entry"):
            content = entry.findtext(f"{ATOM_NS}content") or ""
            # Skip resolved incidents
            if "Resolved" in content:
                continue
            title = entry.findtext(f"{ATOM_NS}title") or "Unknown incident"
            link_el = entry.find(f"{ATOM_NS}link[@rel='alternate']")
            link = link_el.get("href", "") if link_el is not None else ""
            # Extract latest status (first <strong> tag)
            status_match = re.search(r"<strong>(\w+)</strong>", content)
            status = status_match.group(1) if status_match else "Unknown"
            incidents.append({"title": title, "status": status, "link": link})

        return incidents
    except Exception:
        return []


# ── Bar rendering ────────────────────────────────────────────────────

def _bar(fraction, width=20):
    """Render a text progress bar with Unicode block characters."""
    pct = min(max(fraction, 0), 1.0)
    filled = round(pct * width)
    empty = width - filled

    if pct >= 0.8:
        fill_char = "🟥"
    elif pct >= 0.5:
        fill_char = "🟨"
    else:
        fill_char = "🟩"

    return fill_char * filled + "⬜" * empty


def _status_icon(status):
    if status == "allowed":
        return "✅"
    if "warning" in status:
        return "⚠️"
    return "🔴"


# ── Tray App ─────────────────────────────────────────────────────────

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class ClaudeTray:
    def __init__(self):
        create_icons()
        _install_css()

        self.cached_data = None
        self._fetching = False
        self._spinner_idx = 0
        self._spinner_tid = None
        cfg = self._load_config()
        valid_modes = {mode_id for mode_id, _ in self._ALL_PROVIDERS}
        disabled_cfg = cfg.get("disabled_providers", [])
        if not isinstance(disabled_cfg, list):
            disabled_cfg = []
        self._disabled_providers = {
            mode_id for mode_id in disabled_cfg
            if mode_id in valid_modes
        }
        self._mode = cfg.get("active_provider")
        if not isinstance(self._mode, str) or self._mode not in valid_modes:
            self._mode = "claude"
        if self._mode in self._disabled_providers:
            self._mode = next(
                (mode_id for mode_id, _ in self._ALL_PROVIDERS
                 if mode_id not in self._disabled_providers),
                "claude",
            )
        self._disabled_providers.discard(self._mode)

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "claude-usage-tray",
            _icon_path("default", prefix=self._mode),
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(f"{self._mode.capitalize()} Usage")

        self._build_menu()
        self.indicator.set_menu(self.menu)

        # Initial fetch + periodic refresh every 30 min
        self._fetch_bg()
        GLib.timeout_add_seconds(1800, self._fetch_bg)

    def _build_menu(self):
        self.menu = Gtk.Menu()
        self.menu.set_name("usage-menu")

        # ── Header ──
        self.lbl_header = Gtk.MenuItem(label=self._MODE_LABELS[self._mode])
        self.lbl_header.set_sensitive(False)
        self.menu.append(self.lbl_header)

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── 5-Hour section ──
        self.lbl_5h_title = Gtk.MenuItem()
        self.lbl_5h_title.set_sensitive(False)
        self.menu.append(self.lbl_5h_title)

        self.lbl_5h_bar = Gtk.MenuItem()
        self.lbl_5h_bar.set_sensitive(False)
        self.menu.append(self.lbl_5h_bar)

        self.lbl_5h_reset = Gtk.MenuItem()
        self.lbl_5h_reset.set_sensitive(False)
        self.menu.append(self.lbl_5h_reset)

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── 7-Day section ──
        self.lbl_7d_title = Gtk.MenuItem()
        self.lbl_7d_title.set_sensitive(False)
        self.menu.append(self.lbl_7d_title)

        self.lbl_7d_bar = Gtk.MenuItem()
        self.lbl_7d_bar.set_sensitive(False)
        self.menu.append(self.lbl_7d_bar)

        self.lbl_7d_reset = Gtk.MenuItem()
        self.lbl_7d_reset.set_sensitive(False)
        self.menu.append(self.lbl_7d_reset)

        self.lbl_7d_forecast = Gtk.MenuItem()
        self.lbl_7d_forecast.set_sensitive(False)
        self.menu.append(self.lbl_7d_forecast)

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Info ──
        self.lbl_plan = Gtk.MenuItem()
        self.lbl_plan.set_sensitive(False)
        self.menu.append(self.lbl_plan)

        self.lbl_overage = Gtk.MenuItem()
        self.lbl_overage.set_sensitive(False)
        self.menu.append(self.lbl_overage)

        self.lbl_latency = Gtk.MenuItem()
        self.lbl_latency.set_sensitive(False)
        self.menu.append(self.lbl_latency)

        # ── Incidents (dynamic, hidden when empty) ──
        self.incident_sep = Gtk.SeparatorMenuItem()
        self.menu.append(self.incident_sep)
        self.incident_items = []  # will be populated dynamically

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Refresh ──
        self.item_refresh = Gtk.MenuItem(label="⟳ Refresh")
        self.item_refresh.connect("activate", lambda _: self._fetch_bg())
        self.menu.append(self.item_refresh)

        # ── Switch mode ──
        # Sentinel marks the position; the real item is recreated on every rebuild
        # because the AppIndicator/dbusmenu bridge does not drop the submenu arrow
        # when only set_submenu(None) is called on an existing item.
        self._switch_anchor = Gtk.SeparatorMenuItem()
        self._switch_anchor.set_no_show_all(True)
        self._switch_anchor.hide()
        self.menu.append(self._switch_anchor)
        self.item_switch = None

        self._switch_tail_sep = Gtk.SeparatorMenuItem()
        self._switch_tail_sep.set_no_show_all(True)
        self._switch_tail_sep.hide()
        self.menu.append(self._switch_tail_sep)
        self._rebuild_switch_menu()

        # ── Autostart ──
        self.item_autostart = Gtk.MenuItem()
        self._update_autostart_item()
        self.menu.append(self.item_autostart)

        # ── Enable/disable providers ──
        providers_menu = Gtk.Menu()
        self._provider_check_items = {}
        for mode_id, mode_label in self._ALL_PROVIDERS:
            check = Gtk.CheckMenuItem(label=mode_label)
            check.set_active(mode_id not in self._disabled_providers)
            handler_id = check.connect("toggled", self._toggle_provider, mode_id)
            self._provider_check_items[mode_id] = (check, handler_id)
            providers_menu.append(check)
        self._update_provider_check_sensitivity()
        self.item_providers = Gtk.MenuItem(label="Providers…")
        self.item_providers.set_submenu(providers_menu)
        self.menu.append(self.item_providers)

        # ── Quit ──
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _: Gtk.main_quit())
        self.menu.append(item_quit)

        self.menu.show_all()

        # Set initial loading state
        self._set_loading()

    @staticmethod
    def _set_optional(item, text):
        """Set a menu item's label and hide the row entirely if empty."""
        item.set_label(text or "")
        if text:
            item.show()
        else:
            item.hide()

    def _set_loading(self):
        self.lbl_5h_title.set_label("5-Hour Window")
        self.lbl_5h_bar.set_label("Loading…")
        self._set_optional(self.lbl_5h_reset, "")
        self.lbl_7d_title.set_label("7-Day Window")
        self.lbl_7d_bar.set_label("Loading…")
        self._set_optional(self.lbl_7d_reset, "")
        self._set_optional(self.lbl_7d_forecast, "")
        self._set_optional(self.lbl_plan, "")
        self._set_optional(self.lbl_overage, "")
        self._set_optional(self.lbl_latency, "")

    def _update_menu(self, data):
        if isinstance(data, str):
            self.lbl_5h_title.set_label("5-Hour Window")
            self.lbl_5h_bar.set_label(f"⚠️ {data}")
            self._set_optional(self.lbl_5h_reset, "")
            self.lbl_7d_title.set_label("7-Day Window")
            self.lbl_7d_bar.set_label("")
            self._set_optional(self.lbl_7d_reset, "")
            self._set_optional(self.lbl_7d_forecast, "")
            self._set_optional(self.lbl_plan, "")
            self._set_optional(self.lbl_overage, "")
            self._set_optional(self.lbl_latency, "")
            return

        # 5-Hour
        h5 = data["h5_util"]
        self.lbl_5h_title.set_label(
            f"{_status_icon(data['h5_status'])} 5-Hour Window        {h5 * 100:.0f}%"
        )
        self.lbl_5h_bar.set_label(_bar(h5))
        self._set_optional(
            self.lbl_5h_reset,
            f"Resets {_local_time(data['h5_reset'])} · {_time_until(data['h5_reset'])} left"
            if data.get("h5_reset") else "",
        )

        # 7-Day
        d7 = data["d7_util"]
        self.lbl_7d_title.set_label(
            f"{_status_icon(data['d7_status'])} 7-Day Window         {d7 * 100:.0f}%"
        )
        self.lbl_7d_bar.set_label(_bar(d7))
        if data.get("d7_reset"):
            self._set_optional(
                self.lbl_7d_reset,
                f"Resets {_local_time(data['d7_reset'])} · {_time_until(data['d7_reset'], show_days=True)} left",
            )
            self._set_optional(self.lbl_7d_forecast, self._forecast_7d(d7, data["d7_reset"]))
        else:
            self._set_optional(self.lbl_7d_reset, "")
            self._set_optional(self.lbl_7d_forecast, "")

        # Plan
        self._set_optional(self.lbl_plan, f"Plan: {data['plan']}")

        # Overage
        overage = data.get("overage_status", "")
        if overage:
            reason = {
                "org_level_disabled": "Disabled (org)",
                "extra_usage_disabled": "Not enabled",
                "seat_tier_level_disabled": "N/A for plan",
            }.get(data.get("overage_reason", ""), data.get("overage_reason", ""))
            lbl = reason if overage == "rejected" else overage
            self._set_optional(self.lbl_overage, f"Extra Usage: {lbl}")
        else:
            self._set_optional(self.lbl_overage, "")
        self._set_optional(self.lbl_latency, "")

    # ── Mode switching ──

    _MODE_LABELS = {
        "claude": "Claude Subscription Usage",
        "ollama": "Ollama Subscription Usage",
        "codex": "Codex Subscription Usage",
    }
    _ALL_PROVIDERS = (("claude", "Claude"), ("ollama", "Ollama"), ("codex", "Codex"))

    @staticmethod
    def _config_path():
        return CONFIG_PATH

    def _load_config(self):
        return _load_config_file()

    def _save_config(self):
        cfg = self._load_config()
        cfg["active_provider"] = self._mode
        cfg["disabled_providers"] = sorted(self._disabled_providers)
        _save_config_file(cfg)

    def _rebuild_switch_menu(self):
        targets = [
            (mid, label) for mid, label in self._ALL_PROVIDERS
            if mid != self._mode and mid not in self._disabled_providers
        ]

        # Remove the previously-rendered Switch-to item (if any). Recreating from
        # scratch is required because dbusmenu keeps the submenu indicator after
        # set_submenu(None).
        if self.item_switch is not None:
            self.menu.remove(self.item_switch)
            self.item_switch = None

        if not targets:
            self._switch_anchor.hide()
            self._switch_tail_sep.hide()
            return

        self._switch_anchor.show()
        self._switch_tail_sep.show()
        children = self.menu.get_children()
        position = children.index(self._switch_anchor) + 1

        if len(targets) == 1:
            mode_id, mode_label = targets[0]
            new_item = Gtk.MenuItem(label=f"Switch to {mode_label}")
            new_item.connect("activate", self._switch_mode, mode_id)
        else:
            submenu = Gtk.Menu()
            for mode_id, mode_label in targets:
                child = Gtk.MenuItem(label=mode_label)
                child.connect("activate", self._switch_mode, mode_id)
                submenu.append(child)
            submenu.show_all()
            new_item = Gtk.MenuItem(label="Switch to…")
            new_item.set_submenu(submenu)

        new_item.show()
        self.menu.insert(new_item, position)
        self.item_switch = new_item

    def _update_provider_check_sensitivity(self):
        for mode_id, (check, _) in self._provider_check_items.items():
            check.set_sensitive(mode_id != self._mode)

    def _toggle_provider(self, check, mode_id):
        enabled = check.get_active()
        if not enabled and mode_id == self._mode:
            # Active provider can't be disabled — revert silently.
            handler_id = self._provider_check_items[mode_id][1]
            check.handler_block(handler_id)
            check.set_active(True)
            check.handler_unblock(handler_id)
            return
        if enabled:
            self._disabled_providers.discard(mode_id)
        else:
            self._disabled_providers.add(mode_id)
        self._save_config()
        self._rebuild_switch_menu()

    def _switch_mode(self, _widget, mode_id):
        if mode_id == self._mode:
            return
        self._mode = mode_id
        self._disabled_providers.discard(self._mode)
        self._save_config()
        self.lbl_header.set_label(self._MODE_LABELS[mode_id])
        self.indicator.set_title(f"{mode_id.capitalize()} Usage")
        self._update_provider_check_sensitivity()
        self._rebuild_switch_menu()
        self._set_loading()
        self._fetch_bg()

    def _update_menu_ollama(self, data):
        if isinstance(data, str):
            self.lbl_5h_title.set_label("5-Hour Window")
            self.lbl_5h_bar.set_label(f"⚠️ {data}")
            self._set_optional(self.lbl_5h_reset, "")
            self.lbl_7d_title.set_label("7-Day Window")
            self.lbl_7d_bar.set_label("")
            self._set_optional(self.lbl_7d_reset, "")
            self._set_optional(self.lbl_7d_forecast, "")
            self._set_optional(self.lbl_plan, "")
            self._set_optional(self.lbl_overage, "")
            self._set_optional(self.lbl_latency, "")
            return

        # Session usage
        session = data["session_pct"] / 100.0
        session_icon = "✅" if session < 0.5 else ("⚠️" if session < 0.8 else "🔴")
        self.lbl_5h_title.set_label(
            f"{session_icon} 5-Hour Window        {data['session_pct']:.0f}%"
        )
        self.lbl_5h_bar.set_label(_bar(session))
        self._set_optional(
            self.lbl_5h_reset,
            f"Resets in {data['session_reset']}" if data.get("session_reset") else "",
        )

        # Weekly usage
        weekly = data["weekly_pct"] / 100.0
        weekly_icon = "✅" if weekly < 0.5 else ("⚠️" if weekly < 0.8 else "🔴")
        self.lbl_7d_title.set_label(
            f"{weekly_icon} 7-Day Window         {data['weekly_pct']:.1f}%"
        )
        self.lbl_7d_bar.set_label(_bar(weekly))
        self._set_optional(
            self.lbl_7d_reset,
            f"Resets in {data['weekly_reset']}" if data.get("weekly_reset") else "",
        )
        self._set_optional(self.lbl_7d_forecast, "")

        # Plan
        self._set_optional(self.lbl_plan, f"Plan: {data['plan']}")
        self._set_optional(self.lbl_overage, "")
        self._set_optional(self.lbl_latency, "")

    def _update_codex_latency_rows(self, latency):
        if not latency:
            self._set_optional(self.lbl_latency, "")
            return

        current = latency.get("current") or {}
        warning = current.get("warning") or current.get("timeout")
        if warning:
            self._set_optional(self.lbl_latency, "⚠️ Speed: slower than usual")
        else:
            self._set_optional(self.lbl_latency, "✅ Speed: normal")

    def _update_menu_codex(self, data):
        if isinstance(data, str):
            self.lbl_5h_title.set_label("5-Hour Window")
            self.lbl_5h_bar.set_label(f"⚠️ {data}")
            self._set_optional(self.lbl_5h_reset, "")
            self.lbl_7d_title.set_label("7-Day Window")
            self.lbl_7d_bar.set_label("")
            self._set_optional(self.lbl_7d_reset, "")
            self._set_optional(self.lbl_7d_forecast, "")
            self._set_optional(self.lbl_plan, "")
            self._set_optional(self.lbl_overage, "")
            self._set_optional(self.lbl_latency, "")
            return

        # Primary window (e.g. 5h)
        p_pct = data["primary_pct"] / 100.0
        p_icon = "✅" if p_pct < 0.5 else ("⚠️" if p_pct < 0.8 else "🔴")
        self.lbl_5h_title.set_label(
            f"{p_icon} 5-Hour Window        {data['primary_pct']}%"
        )
        self.lbl_5h_bar.set_label(_bar(p_pct))
        self._set_optional(
            self.lbl_5h_reset,
            f"Resets {_local_time(data['primary_reset_ts'])} · {_time_until(data['primary_reset_ts'])} left"
            if data.get("primary_reset_ts") else "",
        )

        # Secondary window (e.g. 7d)
        s_pct = data["secondary_pct"] / 100.0
        s_icon = "✅" if s_pct < 0.5 else ("⚠️" if s_pct < 0.8 else "🔴")
        self.lbl_7d_title.set_label(
            f"{s_icon} 7-Day Window         {data['secondary_pct']}%"
        )
        self.lbl_7d_bar.set_label(_bar(s_pct))
        self._set_optional(
            self.lbl_7d_reset,
            f"Resets {_local_time(data['secondary_reset_ts'])} · {_time_until(data['secondary_reset_ts'], show_days=True)} left"
            if data.get("secondary_reset_ts") else "",
        )
        if data.get("secondary_reset_ts") and data.get("secondary_window_min"):
            self._set_optional(
                self.lbl_7d_forecast,
                self._forecast_window(
                    s_pct, data["secondary_reset_ts"], data["secondary_window_min"] * 60,
                ),
            )
        else:
            self._set_optional(self.lbl_7d_forecast, "")

        # Plan & status
        self._set_optional(self.lbl_plan, f"Plan: {data['plan']}")
        self._set_optional(
            self.lbl_overage,
            "🔴 Rate limit reached!" if data.get("limit_reached") else "",
        )
        self._update_codex_latency_rows(data.get("latency"))

    # ── Window forecast ──

    @staticmethod
    def _forecast_window(util, reset_ts, period_seconds):
        """Predict if a usage limit will be hit at the current rate within `period_seconds`."""
        now = time.time()
        remaining = reset_ts - now
        elapsed = period_seconds - remaining

        if elapsed <= 0 or util <= 0:
            return ""

        projected = util / elapsed * period_seconds
        if projected >= 1.0:
            secs_to_full = (1.0 - util) / (util / elapsed)
            if secs_to_full <= 0:
                return "⚠️ Limit already reached"
            hours = int(secs_to_full // 3600)
            mins = int((secs_to_full % 3600) // 60)
            if hours > 24:
                days = hours // 24
                return f"⚠️ Limit reached in ~{days}d {hours % 24}h at this pace"
            return f"⚠️ Limit reached in ~{hours}h {mins}m at this pace"
        else:
            pct = projected * 100
            return f"✅ ~{pct:.0f}% projected by reset at this pace"

    @classmethod
    def _forecast_7d(cls, util, reset_ts):
        return cls._forecast_window(util, reset_ts, 7 * 24 * 3600)

    # ── Incidents ──

    def _update_incidents(self, incidents):
        # Remove old incident items from menu
        for item in self.incident_items:
            self.menu.remove(item)
        self.incident_items = []

        self._incidents = incidents

        if not incidents:
            self.incident_sep.hide()
            return

        self.incident_sep.show()
        # Insert incident items after the separator
        insert_pos = list(self.menu.get_children()).index(self.incident_sep) + 1

        header = Gtk.MenuItem(label="⚠️ Active Incidents")
        header.set_sensitive(False)
        self.menu.insert(header, insert_pos)
        header.show()
        self.incident_items.append(header)

        for i, inc in enumerate(incidents):
            lbl = Gtk.MenuItem(label=f"  {inc['status']}: {inc['title']}")
            link = inc.get("link", "")
            if link:
                lbl.connect("activate", self._open_link, link)
            else:
                lbl.set_sensitive(False)
            self.menu.insert(lbl, insert_pos + 1 + i)
            lbl.show()
            self.incident_items.append(lbl)

    @staticmethod
    def _open_link(_widget, url):
        import webbrowser
        webbrowser.open(url)

    # ── Autostart ──

    @staticmethod
    def _autostart_path():
        return Path.home() / ".config" / "autostart" / "claude-status-tray.desktop"

    @staticmethod
    def _is_autostart_enabled():
        desktop = Path.home() / ".config" / "autostart" / "claude-status-tray.desktop"
        return desktop.exists()

    def _update_autostart_item(self):
        if self._is_autostart_enabled():
            self.item_autostart.set_label("✓ Autostart enabled")
            self.item_autostart.set_sensitive(False)
        else:
            self.item_autostart.set_label("Add to autostart")
            self.item_autostart.set_sensitive(True)
            try:
                self.item_autostart.disconnect_by_func(self._enable_autostart)
            except TypeError:
                pass
            self.item_autostart.connect("activate", self._enable_autostart)

    def _enable_autostart(self, _widget):
        script_path = Path(os.path.abspath(__file__))
        app_dir = script_path.parent
        desktop_dir = Path.home() / ".config" / "autostart"
        desktop_dir.mkdir(parents=True, exist_ok=True)
        exec_cmd = f"cd {shlex.quote(str(app_dir))} && exec python3 {shlex.quote(str(script_path))}"
        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Claude Status Tray\n"
            f"Exec=bash -lc {shlex.quote(exec_cmd)}\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        self._autostart_path().write_text(desktop_content)
        self._update_autostart_item()

    # ── Refresh with spinner ──

    def _fetch_bg(self):
        if self._fetching:
            return True
        self._fetching = True
        self._spinner_idx = 0
        self._spinner_tid = GLib.timeout_add(80, self._spin_tick)

        mode = self._mode

        def worker():
            if mode == "ollama":
                data = fetch_ollama_usage()
                incidents = []
            elif mode == "codex":
                data = fetch_codex_usage()
                incidents = []
            else:
                data = fetch_usage_data()
                incidents = fetch_incidents()
            GLib.idle_add(self._on_data, mode, data, incidents)
        threading.Thread(target=worker, daemon=True).start()
        return True  # keep periodic timer

    def _spin_tick(self):
        frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        self.item_refresh.set_label(f"{frame} Refreshing…")
        self._spinner_idx += 1
        return True

    def _on_data(self, mode, data, incidents):
        if self._spinner_tid:
            GLib.source_remove(self._spinner_tid)
            self._spinner_tid = None
        self.item_refresh.set_label("⟳ Refresh")
        self._fetching = False
        if mode != self._mode:
            self._fetch_bg()
            return False
        self.cached_data = data
        if self._mode == "ollama":
            self._update_menu_ollama(data)
        elif self._mode == "codex":
            self._update_menu_codex(data)
        else:
            self._update_menu(data)
        self._update_incidents(incidents)
        self._update_icon(data)
        return False

    def _update_icon(self, data):
        has_incidents = bool(getattr(self, "_incidents", None))
        prefix = self._mode
        title = f"{self._mode.capitalize()} Usage"

        if isinstance(data, str):
            self.indicator.set_icon_full(
                _icon_path("default", alert=has_incidents, prefix=prefix), title
            )
            return

        if self._mode == "ollama":
            pct = data.get("session_pct", 0) / 100.0
        elif self._mode == "codex":
            pct = data.get("primary_pct", 0) / 100.0
        else:
            pct = data.get("h5_util", 0)

        if pct >= 0.8:
            color = "red"
        elif pct >= 0.5:
            color = "orange"
        else:
            color = "green"
        self.indicator.set_icon_full(
            _icon_path(color, alert=has_incidents, prefix=prefix), title
        )


# ── Main ─────────────────────────────────────────────────────────────

def main():
    app = ClaudeTray()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
