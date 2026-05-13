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
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Shared helpers ──────────────────────────────────────────────────

CONFIG_PATH = Path.home() / ".config" / "claude-status-tray" / "config.json"


def _load_config_file():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config_file(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


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
            stdin=subprocess.DEVNULL, env=env,
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
    formatted = {
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
    if parsed.get("latency"):
        formatted["latency"] = parsed["latency"]
    return formatted


def fetch_codex():
    codex_bin = _resolve_codex_binary()
    if not codex_bin:
        return {"error": "'codex' not found in PATH."}
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
            return {"error": "Timeout fetching Codex data."}
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
                return {"error": "Timeout fetching Codex data."}

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
            return {"error": "No rate limit data from Codex CLI."}
        parsed["latency"] = latency
        return _format_codex(parsed)
    except FileNotFoundError:
        return {"error": "'codex' not found in PATH."}
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
