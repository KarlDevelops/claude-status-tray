#!/usr/bin/env python3
"""Claude Usage Tray Icon - Shows Claude rate limits in the system tray."""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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


def _icon_path(color, alert=False):
    suffix = "-alert" if alert else ""
    return f"{ICON_DIR}/claude-tray-{color}{suffix}.svg"


def create_icons():
    for name, hex_color in ICON_COLORS.items():
        for alert in (False, True):
            path = _icon_path(name, alert)
            if alert:
                letter, size, y, stroke = "!", 48, 48, 2
            else:
                letter, size, y, stroke = "C", 36, 44, 0
            with open(path, "w") as f:
                f.write(ICON_TEMPLATE.format(
                    color=hex_color, letter=letter,
                    size=size, y=y, stroke=stroke,
                ))


# ── Data fetching ────────────────────────────────────────────────────

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

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "claude-usage-tray",
            _icon_path("default"),
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Claude Usage")

        self._build_menu()
        self.indicator.set_menu(self.menu)

        # Initial fetch + periodic refresh every 5 min
        self._fetch_bg()
        GLib.timeout_add_seconds(1800, self._fetch_bg)

    def _build_menu(self):
        self.menu = Gtk.Menu()
        self.menu.set_name("usage-menu")
        self.menu.connect("show", lambda _: self._fetch_bg())

        # ── Header ──
        header = Gtk.MenuItem(label="Claude Subscription Usage")
        header.set_sensitive(False)
        self.menu.append(header)

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

        # ── Incidents (dynamic, hidden when empty) ──
        self.incident_sep = Gtk.SeparatorMenuItem()
        self.menu.append(self.incident_sep)
        self.incident_items = []  # will be populated dynamically

        self.menu.append(Gtk.SeparatorMenuItem())

        # ── Refresh ──
        self.item_refresh = Gtk.MenuItem(label="⟳ Refresh")
        self.item_refresh.connect("activate", lambda _: self._fetch_bg())
        self.menu.append(self.item_refresh)

        # ── Autostart ──
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item_autostart = Gtk.MenuItem()
        self._update_autostart_item()
        self.menu.append(self.item_autostart)

        # ── Quit ──
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _: Gtk.main_quit())
        self.menu.append(item_quit)

        self.menu.show_all()

        # Set initial loading state
        self._set_loading()

    def _set_loading(self):
        self.lbl_5h_title.set_label("5-Hour Window")
        self.lbl_5h_bar.set_label("Loading…")
        self.lbl_5h_reset.set_label("")
        self.lbl_7d_title.set_label("7-Day Window")
        self.lbl_7d_bar.set_label("Loading…")
        self.lbl_7d_reset.set_label("")
        self.lbl_7d_forecast.set_label("")
        self.lbl_plan.set_label("")
        self.lbl_overage.set_label("")

    def _update_menu(self, data):
        if isinstance(data, str):
            self.lbl_5h_title.set_label("5-Hour Window")
            self.lbl_5h_bar.set_label(f"⚠️ {data}")
            self.lbl_5h_reset.set_label("")
            self.lbl_7d_title.set_label("7-Day Window")
            self.lbl_7d_bar.set_label("")
            self.lbl_7d_reset.set_label("")
            self.lbl_7d_forecast.set_label("")
            self.lbl_plan.set_label("")
            self.lbl_overage.set_label("")
            return

        # 5-Hour
        h5 = data["h5_util"]
        self.lbl_5h_title.set_label(
            f"{_status_icon(data['h5_status'])} 5-Hour Window        {h5 * 100:.0f}%"
        )
        self.lbl_5h_bar.set_label(_bar(h5))
        if data.get("h5_reset"):
            self.lbl_5h_reset.set_label(
                f"Resets {_local_time(data['h5_reset'])} · {_time_until(data['h5_reset'])} left"
            )
        else:
            self.lbl_5h_reset.set_label("")

        # 7-Day
        d7 = data["d7_util"]
        self.lbl_7d_title.set_label(
            f"{_status_icon(data['d7_status'])} 7-Day Window         {d7 * 100:.0f}%"
        )
        self.lbl_7d_bar.set_label(_bar(d7))
        if data.get("d7_reset"):
            self.lbl_7d_reset.set_label(
                f"Resets {_local_time(data['d7_reset'])} · {_time_until(data['d7_reset'], show_days=True)} left"
            )
            self.lbl_7d_forecast.set_label(self._forecast_7d(d7, data["d7_reset"]))
        else:
            self.lbl_7d_reset.set_label("")
            self.lbl_7d_forecast.set_label("")

        # Plan
        self.lbl_plan.set_label(f"Plan: {data['plan']}")

        # Overage
        overage = data.get("overage_status", "")
        if overage:
            reason = {
                "org_level_disabled": "Disabled (org)",
                "extra_usage_disabled": "Not enabled",
                "seat_tier_level_disabled": "N/A for plan",
            }.get(data.get("overage_reason", ""), data.get("overage_reason", ""))
            lbl = reason if overage == "rejected" else overage
            self.lbl_overage.set_label(f"Extra Usage: {lbl}")
        else:
            self.lbl_overage.set_label("")

    # ── 7-Day forecast ──

    @staticmethod
    def _forecast_7d(util, reset_ts):
        """Predict if 7d limit will be hit based on current usage rate."""
        PERIOD = 7 * 24 * 3600  # 7 days in seconds
        now = time.time()
        remaining = reset_ts - now
        elapsed = PERIOD - remaining

        if elapsed <= 0 or util <= 0:
            return ""

        projected = util / elapsed * PERIOD
        if projected >= 1.0:
            # Estimate when 100% is reached
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
        desktop_dir = Path.home() / ".config" / "autostart"
        desktop_dir.mkdir(parents=True, exist_ok=True)
        desktop_content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Claude Status Tray\n"
            f"Exec=python3 {script_path}\n"
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

        def worker():
            data = fetch_usage_data()
            incidents = fetch_incidents()
            GLib.idle_add(self._on_data, data, incidents)
        threading.Thread(target=worker, daemon=True).start()
        return True  # keep periodic timer

    def _spin_tick(self):
        frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        self.item_refresh.set_label(f"{frame} Refreshing…")
        self._spinner_idx += 1
        return True

    def _on_data(self, data, incidents):
        if self._spinner_tid:
            GLib.source_remove(self._spinner_tid)
            self._spinner_tid = None
        self.item_refresh.set_label("⟳ Refresh")
        self._fetching = False
        self.cached_data = data
        self._update_menu(data)
        self._update_incidents(incidents)
        self._update_icon(data)

    def _update_icon(self, data):
        has_incidents = bool(getattr(self, "_incidents", None))

        if isinstance(data, str):
            self.indicator.set_icon_full(
                _icon_path("default", alert=has_incidents), "Claude Usage"
            )
            return
        h5 = data.get("h5_util", 0)
        if h5 >= 0.8:
            color = "red"
        elif h5 >= 0.5:
            color = "orange"
        else:
            color = "green"
        self.indicator.set_icon_full(
            _icon_path(color, alert=has_incidents), "Claude Usage"
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
