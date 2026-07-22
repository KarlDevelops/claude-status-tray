import json
import unittest

import usage_cli


def _event(rate_limits, plan_type="pro"):
    payload = {
        "type": "codex.rate_limits",
        "plan_type": plan_type,
        "rate_limits": rate_limits,
    }
    return "TRACE Received message " + json.dumps(payload, separators=(",", ":"))


class CodexRateLimitParserTests(unittest.TestCase):
    def test_accepts_null_secondary_window(self):
        parsed = usage_cli._parse_codex_rate_limits_legacy(_event({
            "allowed": True,
            "limit_reached": False,
            "primary": {
                "used_percent": 17,
                "window_minutes": 10080,
                "reset_at": 1900000000,
            },
            "secondary": None,
        }))

        self.assertEqual(parsed["primary_pct"], 17)
        self.assertEqual(parsed["primary_window_min"], 10080)
        self.assertFalse(parsed["secondary_available"])
        self.assertIsNone(parsed["secondary_pct"])

        formatted = usage_cli._format_codex(parsed)
        self.assertEqual(formatted["primary"]["label"], "7d")
        self.assertIsNone(formatted["secondary"])

    def test_keeps_both_windows_when_present(self):
        parsed = usage_cli._parse_codex_rate_limits_legacy(_event({
            "primary": {"used_percent": 25, "window_minutes": 300},
            "secondary": {"used_percent": 50, "window_minutes": 10080},
        }))

        self.assertTrue(parsed["primary_available"])
        self.assertTrue(parsed["secondary_available"])
        self.assertEqual(parsed["primary_window_min"], 300)
        self.assertEqual(parsed["secondary_window_min"], 10080)

    def test_uses_latest_complete_event(self):
        first = _event({
            "primary": {"used_percent": 10, "window_minutes": 300},
            "secondary": None,
        })
        second = _event({
            "primary": {"used_percent": 20, "window_minutes": 300},
            "secondary": None,
        })

        parsed = usage_cli._parse_codex_rate_limits_legacy(first + "\n" + second)

        self.assertEqual(parsed["primary_pct"], 20)

    def test_rejects_event_without_usage_windows(self):
        parsed = usage_cli._parse_codex_rate_limits_legacy(_event({
            "allowed": True,
            "primary": None,
            "secondary": None,
        }))

        self.assertIsNone(parsed)

    def test_header_parser_does_not_invent_secondary_window(self):
        parsed = usage_cli._parse_codex_rate_limits_headers(
            '"x-codex-plan-type":"pro" '
            '"x-codex-primary-used-percent":"7" '
            '"x-codex-primary-window-minutes":"10080"'
        )

        self.assertTrue(parsed["primary_available"])
        self.assertFalse(parsed["secondary_available"])
        self.assertIsNone(parsed["secondary_window_min"])


if __name__ == "__main__":
    unittest.main()
