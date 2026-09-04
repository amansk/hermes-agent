"""Tests for chat-mediated MCP OAuth (``tools/mcp_oauth_chat.py``).

The load-bearing invariant is the asymmetry that lets this run ahead of normal
gateway dispatch: an armed login consumes a message ONLY when that message is
actually an OAuth redirect. Everything else — ordinary conversation, a paste
from a different participant — must fall through untouched.

No OAuth machinery runs here; the ``tui_gateway.mcp_oauth_sessions`` primitives
are stubbed, since what is under test is the chat layer wrapped around them.
"""
from __future__ import annotations

import pytest

from tools import mcp_oauth_chat
from tools.mcp_oauth import parse_oauth_redirect

SESSION = "telegram:chat-1"
GOOD_URL = "http://127.0.0.1:8765/callback?code=abc123&state=st-1"


@pytest.fixture(autouse=True)
def _clean_registry():
    mcp_oauth_chat.clear(SESSION)
    yield
    mcp_oauth_chat.clear(SESSION)


def _arm(user_id="user-1", session_id="sess-1", server_name="linear"):
    """Register a pending paste directly, bypassing start_flow."""
    with mcp_oauth_chat._lock:
        mcp_oauth_chat._pending[SESSION] = {
            "session_id": session_id,
            "server_name": server_name,
            "user_id": user_id,
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "created_at": __import__("time").time(),
        }


def _stub_sessions(monkeypatch, *, deliver=None, poll=None, cancel=None):
    """Patch the session primitives the module imports lazily."""
    import tui_gateway.mcp_oauth_sessions as sessions

    calls = {"deliver": [], "poll": [], "cancel": []}

    def _deliver(session_id, server_name, *, code, state, error=None):
        calls["deliver"].append({"code": code, "state": state, "error": error})
        return deliver if deliver is not None else {"ok": True}

    def _poll(session_id, server_name):
        calls["poll"].append(session_id)
        return poll if poll is not None else {"status": "approved", "tools": []}

    def _cancel(session_id, server_name):
        calls["cancel"].append(session_id)
        return cancel if cancel is not None else {"ok": True}

    monkeypatch.setattr(sessions, "deliver_callback_flow", _deliver)
    monkeypatch.setattr(sessions, "poll_flow", _poll)
    monkeypatch.setattr(sessions, "cancel_flow", _cancel)
    return calls


class TestRedirectParsing:
    """parse_oauth_redirect is the gate that keeps chat messages from being
    swallowed, so it has to be exact in both directions."""

    @pytest.mark.parametrize(
        "text",
        [
            "http://127.0.0.1:8765/callback?code=abc&state=st",
            "https://mcp.example.com/callback?code=abc&state=st",
            "?code=abc&state=st",
            "code=abc&state=st",
        ],
    )
    def test_accepts_every_paste_shape(self, text):
        parsed = parse_oauth_redirect(text)
        assert parsed is not None
        assert parsed["code"] == "abc"
        assert parsed["state"] == "st"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "what's the weather",
            "https://example.com/some/page",
            "check the code= in that file",  # bare 'code=' with no value
            "/reload-mcp",
        ],
    )
    def test_rejects_everything_that_is_not_a_redirect(self, text):
        assert parse_oauth_redirect(text) is None

    def test_error_redirect_parses_without_a_code(self):
        parsed = parse_oauth_redirect("?error=access_denied&state=st")
        assert parsed is not None
        assert parsed["error"] == "access_denied"
        assert parsed["code"] is None


class TestPasteResolution:
    @pytest.mark.asyncio
    async def test_ordinary_message_falls_through(self, monkeypatch):
        calls = _stub_sessions(monkeypatch)
        _arm()
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-1", text="can you check the deploy?"
        )
        assert result is None, "an armed login must not swallow chat"
        assert calls["deliver"] == []
        # Still armed — the user can paste later.
        assert mcp_oauth_chat.get_pending(SESSION) is not None

    @pytest.mark.asyncio
    async def test_no_pending_login_is_a_no_op(self, monkeypatch):
        _stub_sessions(monkeypatch)
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-1", text=GOOD_URL
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_another_participant_cannot_complete_the_login(self, monkeypatch):
        calls = _stub_sessions(monkeypatch)
        _arm(user_id="user-1")
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-2", text=GOOD_URL
        )
        assert result is None
        assert calls["deliver"] == [], "a non-owner's paste must not be delivered"
        assert mcp_oauth_chat.get_pending(SESSION) is not None

    @pytest.mark.asyncio
    async def test_good_paste_connects_and_reports_tool_count(self, monkeypatch):
        calls = _stub_sessions(
            monkeypatch,
            poll={"status": "approved", "tools": [{"name": "a"}, {"name": "b"}]},
        )
        _arm()
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-1", text=GOOD_URL
        )
        assert result == "linear connected. 2 tools available."
        assert calls["deliver"] == [{"code": "abc123", "state": "st-1", "error": None}]
        assert mcp_oauth_chat.get_pending(SESSION) is None

    @pytest.mark.asyncio
    async def test_state_mismatch_does_not_echo_flow_internals(self, monkeypatch):
        _stub_sessions(
            monkeypatch,
            deliver={"ok": False, "error_message": "OAuth callback state mismatch"},
        )
        _arm()
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-1", text=GOOD_URL
        )
        assert "state mismatch" not in result
        assert "/mcp-login linear" in result
        assert mcp_oauth_chat.get_pending(SESSION) is None

    @pytest.mark.asyncio
    async def test_provider_error_redirect_is_reported(self, monkeypatch):
        calls = _stub_sessions(monkeypatch)
        _arm()
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-1", text="?error=access_denied&state=st-1"
        )
        assert "access_denied" in result
        assert calls["deliver"] == [], "a denial is not delivered as a callback"
        assert calls["cancel"] == ["sess-1"], "the flow must be torn down"

    @pytest.mark.asyncio
    async def test_skip_token_cancels_and_tears_down_the_flow(self, monkeypatch):
        calls = _stub_sessions(monkeypatch)
        _arm()
        result = await mcp_oauth_chat.resolve_paste(
            SESSION, user_id="user-1", text="cancel"
        )
        assert "Cancelled" in result
        assert calls["cancel"] == ["sess-1"]
        assert mcp_oauth_chat.get_pending(SESSION) is None


class TestStaleness:
    def test_stale_paste_is_dropped_and_its_flow_abandoned(self, monkeypatch):
        calls = _stub_sessions(monkeypatch)
        _arm()
        with mcp_oauth_chat._lock:
            mcp_oauth_chat._pending[SESSION]["created_at"] = 0.0

        assert mcp_oauth_chat.clear_if_stale(SESSION) is True
        assert mcp_oauth_chat.get_pending(SESSION) is None
        # A cleared-but-live flow would block the user's next /mcp-login.
        assert calls["cancel"] == ["sess-1"]

    def test_fresh_paste_survives(self, monkeypatch):
        _stub_sessions(monkeypatch)
        _arm()
        assert mcp_oauth_chat.clear_if_stale(SESSION) is False
        assert mcp_oauth_chat.get_pending(SESSION) is not None
