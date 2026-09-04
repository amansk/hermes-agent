"""Chat-mediated MCP OAuth for gateways with no reachable callback URL.

The built-in flow assumes the browser that finishes authorization can reach
the Hermes process: it registers ``http://127.0.0.1:<port>/callback`` and binds
a listener there (``tools/mcp_oauth.py``). On a messaging gateway both halves
of that assumption fail — the user's browser is on their phone, and there is no
TTY for the stdin paste fallback to read. ``/reload-mcp`` therefore raises
``OAuthNonInteractiveError`` telling the user to run a CLI command on a host
they may have no shell on.

This module moves that same paste fallback onto the chat channel. Hermes sends
the authorization URL as a message, the user authorizes on their phone, their
browser fails to load the loopback redirect (expected), and they paste the
address bar back into the chat. Nothing new is registered publicly and no
listener is bound: the redirect URI is a loopback address deliberately left
unanswered, exactly as in the ``mcp-oauth-remote-gateway`` skill's manual
procedure — which this module exists to make unnecessary.

No OAuth logic is reimplemented here. Discovery, DCR, PKCE, state validation
and token exchange all stay in the MCP SDK, driven by the session primitives in
``tui_gateway.mcp_oauth_sessions`` (transport-neutral plain functions despite
living under ``tui_gateway``) over the ``DashboardOAuthFlow`` rendezvous. This
module contributes two things: a pending-paste registry keyed by gateway
session, mirroring ``tools.slash_confirm``, and the sender binding that keeps
one participant from completing another's handshake.

**The tradeoff this makes explicit:** a pasted redirect puts a live
authorization ``code`` into chat history. It is single-use and bound by PKCE to
a ``code_verifier`` that never leaves this process, so an attacker reading the
message afterwards cannot redeem it — but on a shared or archived channel it is
still an exposure. A gateway whose dashboard is publicly reachable should drive
OAuth from there instead; this path is for the deployments where that is not an
option.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The loopback port baked into the redirect URI. Nothing binds it — the user's
# browser is meant to fail to connect so they copy the address bar. A fixed
# port keeps the redirect URI stable across re-auths of the same server, which
# matters because providers reject a redirect URI that differs from the one
# pinned at dynamic client registration.
DEFAULT_PASTE_REDIRECT_PORT = 8765

# How long a pending paste survives. Generous on purpose: the user has to leave
# the chat app, authorize in a browser, and come back.
PASTE_TIMEOUT_SECONDS = 900

# How long to wait for the worker to finish token exchange after a good paste.
# This is held inside the message handler, so it also bounds how long a chat
# turn can block. The exchange itself is one round trip; the rest is the
# server's own connect + tools/list, and past this the user is better served by
# a "run /reload-mcp" nudge than by a stalled conversation.
COMPLETION_TIMEOUT_SECONDS = 120

# Pending pastes keyed by gateway session_key. One per session; starting a new
# login supersedes a stale one.
_pending: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()


def _redirect_uri_for(server_name: str, hermes_home: str) -> str:
    """Pick the loopback redirect URI to register for this server.

    Reuses the redirect URI from a cached client registration when one exists.
    A server the user previously authorized from the CLI has a ``client_id``
    pinned to that flow's port; presenting a different URI now would be
    rejected by the authorization server as a redirect mismatch. Only loopback
    URIs are reused — a cached public URI belongs to a dashboard flow and is
    not reachable in this deployment.
    """
    try:
        from tools.mcp_oauth import HermesTokenStorage, _cached_redirect_uri

        cached = _cached_redirect_uri(
            HermesTokenStorage(server_name, hermes_home=hermes_home)
        )
        if cached:
            host = (urlparse(cached).hostname or "").lower()
            if host in ("127.0.0.1", "localhost", "::1"):
                return cached
    except Exception as exc:  # cache is an optimization; never fail the login
        logger.debug("could not read cached redirect_uri for %s: %s", server_name, exc)
    return f"http://127.0.0.1:{DEFAULT_PASTE_REDIRECT_PORT}/callback"


def start(
    session_key: str,
    *,
    server_name: str,
    cfg: dict,
    hermes_home: str,
    user_id: Optional[str] = None,
    reconnect_live: bool = True,
) -> Dict[str, Any]:
    """Begin a chat-mediated OAuth flow and arm the pending paste.

    Returns ``{session_id, auth_url, redirect_uri}``. Raises whatever
    ``start_flow`` raises (``RuntimeError`` for a duplicate or over-cap flow,
    ``TimeoutError`` when the provider never yields an authorization URL) — the
    caller renders those for the user.
    """
    from tui_gateway.mcp_oauth_sessions import start_flow

    redirect_uri = _redirect_uri_for(server_name, hermes_home)
    started = start_flow(
        hermes_home,
        server_name,
        cfg,
        reconnect_live=reconnect_live,
        client_redirect_uri=redirect_uri,
    )
    with _lock:
        _pending[session_key] = {
            "session_id": started["session_id"],
            "server_name": server_name,
            "user_id": user_id,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    return {
        "session_id": started["session_id"],
        "auth_url": started["auth_url"],
        "redirect_uri": redirect_uri,
    }


def get_pending(session_key: str) -> Optional[Dict[str, Any]]:
    """Return the pending paste for a session, or None."""
    with _lock:
        entry = _pending.get(session_key)
        return dict(entry) if entry else None


def clear(session_key: str) -> None:
    """Drop the pending paste for ``session_key`` without resolving it."""
    with _lock:
        _pending.pop(session_key, None)


def cancel(session_key: str) -> bool:
    """Drop the pending paste AND tear down its flow. True if one was armed.

    Prefer this over :func:`clear` whenever the user will not be coming back
    with a paste — a cleared-but-live flow keeps its worker parked on the
    callback wait and keeps ``start_flow``'s duplicate guard rejecting the
    retry the user is most likely to try next.
    """
    with _lock:
        entry = _pending.pop(session_key, None)
    if entry is None:
        return False
    _abandon(entry)
    return True


def clear_if_stale(session_key: str, timeout: float = PASTE_TIMEOUT_SECONDS) -> bool:
    """Drop the pending paste if older than ``timeout``. True if dropped."""
    with _lock:
        entry = _pending.get(session_key)
        if not entry:
            return False
        if time.time() - float(entry.get("created_at", 0) or 0) <= timeout:
            return False
        _pending.pop(session_key, None)
    # Tear the flow down too, outside the lock — an expired paste means the
    # user is not coming back, and a live flow would block their next attempt.
    _abandon(entry)
    return True


def _sender_matches(entry: Dict[str, Any], user_id: Optional[str]) -> bool:
    """Whether *user_id* is the participant who started this login.

    An adapter that reports no identity at all (``user_id`` unset on both
    sides) falls back to session scoping, which is what every other pending-
    state primitive in the gateway does. But once a login was started by an
    identified sender, only that sender can finish it — otherwise a second
    participant in the same chat could complete a handshake that binds the
    operator's account.
    """
    expected = entry.get("user_id")
    if expected is None:
        return True
    return user_id is not None and str(user_id) == str(expected)


async def resolve_paste(
    session_key: str,
    *,
    user_id: Optional[str],
    text: str,
) -> Optional[str]:
    """Try to complete a pending login from a chat message.

    Returns a user-facing message when this message was consumed, or ``None``
    when it was not an OAuth paste — the caller then routes it normally. That
    distinction is the whole contract: an ordinary chat message must never be
    swallowed by an armed login, so anything that does not parse as a redirect
    (or a skip token) falls through untouched.
    """
    from tools.mcp_oauth import is_skip_token, parse_oauth_redirect

    entry = get_pending(session_key)
    if entry is None:
        return None
    if not _sender_matches(entry, user_id):
        return None

    server_name = entry["server_name"]

    if is_skip_token(text):
        cancel(session_key)
        return f"Cancelled the {server_name} login. Nothing was saved."

    parsed = parse_oauth_redirect(text)
    if parsed is None:
        return None

    # From here the message was unambiguously a redirect, so it is ours to
    # answer however it turns out.
    clear(session_key)

    if parsed["error"]:
        _abandon(entry)  # already unregistered above
        return (
            f"{server_name} refused the authorization: {parsed['error']}. "
            f"Run `/mcp-login {server_name}` to try again."
        )

    from tui_gateway.mcp_oauth_sessions import deliver_callback_flow

    delivered = await asyncio.to_thread(
        deliver_callback_flow,
        entry["session_id"],
        server_name,
        code=parsed["code"],
        state=parsed["state"],
        error=None,
    )
    if not delivered.get("ok"):
        # State mismatch, an expired session, or a second paste on a flow that
        # already settled. All of them mean "start over", and none of them
        # should echo the raw reason, which can carry flow internals.
        logger.warning(
            "chat OAuth callback rejected for %s: %s",
            server_name,
            delivered.get("error_message"),
        )
        return (
            f"That link didn't match the {server_name} login I started — it may "
            f"have expired, or come from an older attempt. "
            f"Run `/mcp-login {server_name}` to start a fresh one."
        )

    return await _await_completion(entry["session_id"], server_name)


async def _await_completion(session_id: str, server_name: str) -> str:
    """Wait for the worker to finish token exchange and render the outcome."""
    from tui_gateway.mcp_oauth_sessions import poll_flow

    deadline = time.monotonic() + COMPLETION_TIMEOUT_SECONDS
    status: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = await asyncio.to_thread(poll_flow, session_id, server_name)
        if status.get("status") != "pending":
            break
        await asyncio.sleep(0.25)
    else:
        return (
            f"Authorized, but {server_name} didn't finish connecting in time. "
            f"Run `/reload-mcp` to pick it up, or `/mcp-login {server_name}` "
            f"to retry."
        )

    if status.get("status") == "approved":
        tools = status.get("tools") or []
        count = len(tools)
        suffix = "" if count == 1 else "s"
        return (
            f"{server_name} connected. {count} tool{suffix} available."
            if count
            else f"{server_name} connected."
        )

    detail = status.get("error_message") or "the provider rejected the exchange"
    return f"Couldn't finish the {server_name} login: {detail}"


def _abandon(entry: Dict[str, Any]) -> None:
    """Tear down a flow the user walked away from.

    Without this the worker thread sits on its callback wait for the full
    timeout and the duplicate-flow guard in ``start_flow`` rejects the retry
    the user is most likely to attempt next.
    """
    try:
        from tui_gateway.mcp_oauth_sessions import cancel_flow

        cancel_flow(entry["session_id"], entry["server_name"])
    except Exception as exc:
        logger.debug("could not abandon OAuth flow %s: %s", entry.get("session_id"), exc)
