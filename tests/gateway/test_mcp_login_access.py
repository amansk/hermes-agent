"""Gateway authorization tests for /mcp-login.

An authorization URL posted into a chat is a live invitation to bind the
OPERATOR's accounts to this gateway, and the tokens it yields are usable by
every session on that gateway. Two gates keep that from being reachable by an
ordinary allowed sender: an explicitly-configured admin, and DM-only delivery.
Both must fail closed on the default config, where no admin list is set and
every allowed sender is otherwise treated as unrestricted.
"""

from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _source(user_id: str = "user-1", *, chat_type: str = "dm") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id=user_id,
        chat_id=f"{chat_type}-1",
        chat_type=chat_type,
    )


def _runner(*, admins=(), group_admins=()):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                extra={
                    "allow_admin_from": list(admins),
                    "group_allow_admin_from": list(group_admins),
                },
            )
        }
    )
    runner._session_key_for_source = lambda source: "discord:chat-1"
    return runner


def _event(user_id: str = "user-1", *, chat_type: str = "dm", args: str = "linear"):
    event = MagicMock()
    event.source = _source(user_id, chat_type=chat_type)
    event.get_command_args.return_value = args
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admins", "user_id"),
    [
        ((), "user-1"),          # default config: no admin list at all
        (("admin-1",), "user-1"),  # configured, but this caller isn't on it
    ],
)
async def test_requires_explicit_admin(admins, user_id, monkeypatch):
    """An allowed but non-admin sender must not be able to start a login."""
    from tools import mcp_oauth_chat

    started = []
    monkeypatch.setattr(
        mcp_oauth_chat, "start", lambda *a, **kw: started.append(kw) or {}
    )

    runner = _runner(admins=admins)
    result = await runner._handle_mcp_login_command(_event(user_id))

    assert "requires an explicitly configured gateway admin" in result
    assert started == [], "a non-admin must never reach the OAuth flow"


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "channel", "supergroup"])
async def test_refused_outside_a_dm(chat_type, monkeypatch):
    """Even a real admin cannot run this where a group can read the prompt."""
    from tools import mcp_oauth_chat

    started = []
    monkeypatch.setattr(
        mcp_oauth_chat, "start", lambda *a, **kw: started.append(kw) or {}
    )

    runner = _runner(admins=("user-1",), group_admins=("user-1",))
    result = await runner._handle_mcp_login_command(
        _event("user-1", chat_type=chat_type)
    )

    assert "only runs in a direct message" in result
    assert started == []


@pytest.mark.asyncio
async def test_unknown_server_is_named_with_the_configured_set(monkeypatch):
    """An admin in a DM gets past both gates and into config validation."""
    import hermes_cli.mcp_config as mcp_config

    monkeypatch.setattr(
        mcp_config, "_get_mcp_servers", lambda: {"stripe": {"url": "https://x"}}
    )

    runner = _runner(admins=("user-1",))
    result = await runner._handle_mcp_login_command(_event("user-1", args="linear"))

    assert "No MCP server named 'linear'" in result
    assert "stripe" in result


@pytest.mark.asyncio
async def test_stdio_server_is_rejected_before_starting_a_flow(monkeypatch):
    """stdio servers authenticate via env keys — there is no OAuth to run."""
    import hermes_cli.mcp_config as mcp_config
    from tools import mcp_oauth_chat

    monkeypatch.setattr(
        mcp_config, "_get_mcp_servers", lambda: {"local": {"command": "./serve"}}
    )
    started = []
    monkeypatch.setattr(
        mcp_oauth_chat, "start", lambda *a, **kw: started.append(kw) or {}
    )

    runner = _runner(admins=("user-1",))
    result = await runner._handle_mcp_login_command(_event("user-1", args="local"))

    assert "stdio server" in result
    assert started == []


@pytest.mark.asyncio
async def test_admin_in_dm_gets_the_authorize_url_and_paste_instructions(monkeypatch):
    import hermes_cli.mcp_config as mcp_config
    from tools import mcp_oauth_chat

    monkeypatch.setattr(
        mcp_config, "_get_mcp_servers", lambda: {"linear": {"url": "https://mcp.linear.app"}}
    )
    monkeypatch.setattr(
        mcp_oauth_chat,
        "start",
        lambda *a, **kw: {
            "session_id": "sess-1",
            "auth_url": "https://linear.app/oauth/authorize?state=st",
            "redirect_uri": "http://127.0.0.1:8765/callback",
        },
    )

    runner = _runner(admins=("user-1",))
    result = await runner._handle_mcp_login_command(_event("user-1", args="linear"))

    assert "https://linear.app/oauth/authorize?state=st" in result
    # The user must be told the failed page load is expected, or they will
    # report it as the bug instead of pasting the URL back.
    assert "http://127.0.0.1:8765/callback" in result
    assert "expected" in result
