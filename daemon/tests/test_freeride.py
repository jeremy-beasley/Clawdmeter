#!/usr/bin/env python3
"""Tests for the daemon's pure free-ride credential handling.

The daemon NEVER refreshes the OAuth token — Claude Code (the token's owner) does
all refreshing; the daemon only reads whatever access token is currently stored and,
when it's dead, signals "No data" to the device. This replaces the old self-refresh
test suites (test_macos_refresh.py / test_windows_refresh.py), which covered machinery
that has been removed.

Run: python -m pytest daemon/tests/test_freeride.py -x -q
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from daemon.claude_usage_daemon import _decode_keychain_blob  # noqa: E402


# --- read path: hex-dumped Keychain recovery (kept from the macOS suite) -----

def test_decode_passthrough_plain_json():
    """A normal JSON blob is never valid hex, so it passes through untouched."""
    blob = '{"claudeAiOauth": {"accessToken": "x"}}'
    assert _decode_keychain_blob(blob) == blob


def test_decode_hex_dump_round_trips():
    """An embedded newline makes `security -w` emit hex; we decode it back."""
    original = '{"claudeAiOauth": {"accessToken": "x"}}\n'
    hex_dump = original.encode("utf-8").hex()  # what `security -w` would print
    assert _decode_keychain_blob(hex_dump) == original


def test_decode_odd_length_is_not_treated_as_hex():
    """Odd-length all-hex-looking strings aren't valid hex; pass through."""
    assert _decode_keychain_blob("abc") == "abc"


# --- regression guard: the daemon must NEVER touch the refresh endpoint ------

def test_daemons_have_no_refresh_path():
    """Pure free-ride contract: neither daemon may carry client-side refresh. Guards
    against re-introducing the rotation race / 429-bucket exhaustion we removed."""
    import daemon.claude_usage_daemon as macos
    import daemon.claude_usage_daemon_windows as win
    forbidden = (
        "refresh_access_token", "_apply_refresh_response", "_note_refresh_failure",
        "_note_poll_success", "_credentials_expiry_seconds",
        "OAUTH_TOKEN_URL", "OAUTH_CLIENT_ID", "_refresh_failures", "_last_refresh_attempt",
    )
    for mod in (macos, win):
        present = [s for s in forbidden if hasattr(mod, s)]
        assert not present, f"{mod.__name__} still exposes refresh machinery: {present}"


# --- behavior: a dead token yields a {"ok": false} no-data beat, no refresh --

def _connected_client():
    client = AsyncMock()
    client.connect = AsyncMock(return_value=None)
    client.is_connected = True
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.write_gatt_char = AsyncMock(return_value=None)
    return client


def test_freeride_autherror_emits_no_data_beat():
    """On a 401 (AuthError) the daemon emits a {"ok": false} no-data beat and exits the
    poll cleanly — there is no refresh path to fall back on. The device then shows its
    idle 'No data' screen instead of stale numbers."""
    import daemon.claude_usage_daemon_windows as mod
    device = MagicMock(); device.address = "AA:BB:CC:DD:EE:FF"
    client = _connected_client()
    writes = []

    async def go():
        stop_event = asyncio.Event()

        async def fake_poll(token):
            stop_event.set()          # one poll, then unwind the loop
            raise mod.AuthError(401)

        async def cap_write(uuid, data, response=False):
            writes.append(data)

        client.write_gatt_char = AsyncMock(side_effect=cap_write)
        with patch.object(mod, "BleakClient", return_value=client), \
             patch.object(mod, "read_token", return_value="EXPIRED"), \
             patch.object(mod, "poll_api", new=fake_poll):
            await mod.connect_and_run(device, stop_event)

    asyncio.run(go())
    payloads = [
        json.loads(w.decode() if isinstance(w, (bytes, bytearray)) else w)
        for w in writes
    ]
    assert {"ok": False} in payloads, f"expected a no-data beat, got writes: {payloads}"


def _dead_beat(dead):
    """Drive one dead-token poll and return the JSON payloads written to the device."""
    import daemon.claude_usage_daemon as mod
    device = MagicMock(); device.address = "AA:BB:CC:DD:EE:FF"
    client = _connected_client()
    writes = []

    async def go():
        stop_event = asyncio.Event()

        async def fake_poll_active():
            stop_event.set()          # one poll, then unwind the loop
            return None, dead         # (payload, dead)

        async def cap_write(uuid, data, response=False):
            writes.append(data)

        client.write_gatt_char = AsyncMock(side_effect=cap_write)
        with patch.object(mod, "BleakClient", return_value=client), \
             patch.object(mod, "poll_active", new=fake_poll_active):
            await mod.connect_and_run(device, stop_event)

    asyncio.run(go())
    payloads = [
        json.loads(w.decode() if isinstance(w, (bytes, bytearray)) else w)
        for w in writes
    ]
    return payloads


def test_macos_expired_token_beat_says_why_auth():
    """A present-but-expired token (401) → why:"auth" → firmware shows "Token expired"."""
    assert {"ok": False, "why": "auth"} in _dead_beat("auth")


def test_macos_missing_token_beat_says_why_login():
    """No token at all (logged out / session expired) → why:"login" → firmware shows
    "Login needed"."""
    assert {"ok": False, "why": "login"} in _dead_beat("login")


def test_poll_active_reports_login_vs_auth(monkeypatch):
    """poll_active distinguishes 'no token anywhere' from 'token present but 401'."""
    import daemon.claude_usage_daemon as mod
    from pathlib import Path
    monkeypatch.setattr(mod, "read_config_dirs", lambda: [Path("/x")])

    monkeypatch.setattr(mod, "read_token_for", lambda d: None)
    assert asyncio.run(mod.poll_active())[1] == "login"

    async def expired(token):
        raise mod.TokenExpired()
    monkeypatch.setattr(mod, "read_token_for", lambda d: "tok")
    monkeypatch.setattr(mod, "poll_api", expired)
    assert asyncio.run(mod.poll_active())[1] == "auth"
