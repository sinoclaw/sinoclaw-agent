"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Sinoclaw-Session-Id header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists sinoclaw-agent as an available model
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- GET  /health                     — health check
- GET  /health/detailed            — rich status for cross-container dashboard probing

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to sinoclaw-agent
through this adapter by pointing at http://localhost:8642/v1.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import socket as _socket
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
)

logger = logging.getLogger(__name__)

# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 1_000_000  # 1 MB default limit for POST bodies
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """Normalize OpenAI chat message content into a plain text string.

    Some clients (Open WebUI, LobeChat, etc.) send content as an array of
    typed parts instead of a plain string::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    This function flattens those into a single string so the agent pipeline
    (which expects strings) doesn't choke.

    Defensive limits prevent abuse: recursion depth, list size, and output
    length are all bounded.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    parts.append(item[:MAX_NORMALIZED_TEXT_LENGTH])
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            parts.append(str(text)[:MAX_NORMALIZED_TEXT_LENGTH])
                        except Exception:
                            pass
                # Silently skip image_url / other non-text parts
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
            # Check accumulated size
            if sum(len(p) for p in parts) >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # Fallback for unexpected types (int, float, bool, etc.)
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from sinoclaw_cli.config import get_sinoclaw_home
                db_path = str(get_sinoclaw_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        import time
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        return json.loads(row[0])

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            self._conn.execute(
                "DELETE FROM responses WHERE response_id IN "
                "(SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?)",
                (count - self._max_size,),
            )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """Get the latest response_id for a conversation name."""
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """Map a conversation name to its latest response_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        return await handler(request)
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics."""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        import time as _t
        now = _t.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]
        resp = await compute_coro()
        import time as _t
        self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
        self._purge()
        return resp


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """Derive a stable session ID from the conversation's first user message.

    OpenAI-compatible frontends (Open WebUI, LibreChat, etc.) send the full
    conversation history with every request.  The system prompt and first user
    message are constant across all turns of the same conversation, so hashing
    them produces a deterministic session ID that lets the API server reuse
    the same Sinoclaw session (and therefore the same Docker container sandbox
    directory) across turns.
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through sinoclaw-agent's AIAgent.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))))
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[str, float] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """Derive the advertised model name for /v1/models.

        Priority:
        1. Explicit override (config extra or API_SERVER_MODEL_NAME env var)
        2. Active profile name (so each profile advertises a distinct model)
        3. Fallback: "sinoclaw-agent"
        """
        if explicit and explicit.strip():
            return explicit.strip()
        try:
            from sinoclaw_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in ("default", "custom"):
                return profile
        except Exception:
            pass
        return "sinoclaw-agent"

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        If no API key is configured, all requests are allowed (only when API
        server is local).
        """
        if not self._api_key:
            return None  # No key configured — allow all (local-only use)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._api_key):
                return None  # Auth OK

        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        """Lazily initialise and return the shared SessionDB instance.

        Sessions are persisted to ``state.db`` so that ``sinoclaw sessions list``
        shows API-server conversations alongside CLI and gateway ones.
        """
        if self._session_db is None:
            try:
                from sinoclaw_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.debug("SessionDB unavailable for API server: %s", e)
        return self._session_db

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the sinoclaw-api-server default.
        """
        from run_agent import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model, _load_gateway_config
        from sinoclaw_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = int(os.getenv("SINOCLAW_MAX_ITERATIONS", "90"))

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        from gateway.run import GatewayRunner
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "sinoclaw-agent"})

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed — rich status for cross-container dashboard probing.

        Returns gateway state, connected platforms, PID, and uptime so the
        dashboard can display full status without needing a shared PID file or
        /proc access.  No authentication required.
        """
        from gateway.status import read_runtime_status

        runtime = read_runtime_status() or {}
        return web.json_response({
            "status": "ok",
            "platform": "sinoclaw-agent",
            "gateway_state": runtime.get("gateway_state"),
            "platforms": runtime.get("platforms", {}),
            "active_agents": runtime.get("active_agents", 0),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
            "pid": os.getpid(),
        })

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — return sinoclaw-agent as an available model."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": self._model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "sinoclaw",
                    "permission": [],
                    "root": self._model_name,
                    "parent": None,
                }
            ],
        })

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = body.get("stream", False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = _normalize_chat_content(msg.get("content", ""))
            if role == "system":
                # Accumulate system messages
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in ("user", "assistant"):
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to continue an existing session by passing X-Sinoclaw-Session-Id.
        # When provided, history is loaded from state.db instead of from the request body.
        #
        # Security: session continuation exposes conversation history, so it is
        # only allowed when the API key is configured and the request is
        # authenticated.  Without this gate, any unauthenticated client could
        # read arbitrary session history by guessing/enumerating session IDs.
        provided_session_id = request.headers.get("X-Sinoclaw-Session-Id", "").strip()
        if provided_session_id:
            if not self._api_key:
                logger.warning(
                    "Session continuation via X-Sinoclaw-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity."
                )
                return web.json_response(
                    _openai_error(
                        "Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature."
                    ),
                    status=403,
                )
            # Sanitize: reject control characters that could enable header injection.
            if re.search(r'[\r\n\x00]', provided_session_id):
                return web.json_response(
                    {"error": {"message": "Invalid session ID", "type": "invalid_request_error"}},
                    status=400,
                )
            session_id = provided_session_id
            try:
                db = self._ensure_session_db()
                if db is not None:
                    history = db.get_messages_as_conversation(session_id)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # Derive a stable session ID from the conversation fingerprint so
            # that consecutive messages from the same Open WebUI (or similar)
            # conversation map to the same Sinoclaw session.  The first user
            # message + system prompt are constant across all turns.
            first_user = ""
            for cm in conversation_messages:
                if cm.get("role") == "user":
                    first_user = cm.get("content", "")
                    break
            session_id = _derive_chat_session_id(system_prompt, first_user)
            # history already set from request body above

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._model_name)
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # Filter out None — the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Send tool progress as a separate SSE event.

                Previously, progress markers like ``⏰ list`` were injected
                directly into ``delta.content``.  OpenAI-compatible frontends
                (Open WebUI, LobeChat, …) store ``delta.content`` verbatim as
                the assistant message and send it back on subsequent requests.
                After enough turns the model learns to *emit* the markers as
                plain text instead of issuing real tool calls — silently
                hallucinating tool results.  See #6972.

                The fix: push a tagged tuple ``("__tool_progress__", payload)``
                onto the stream queue.  The SSE writer emits it as a custom
                ``event: sinoclaw.tool.progress`` line that compliant frontends
                can render for UX but will *not* persist into conversation
                history.  Clients that don't understand the custom event type
                silently ignore it per the SSE specification.
                """
                if event_type != "tool.started":
                    return
                if name.startswith("_"):
                    return
                from agent.display import get_tool_emoji
                emoji = get_tool_emoji(name)
                label = preview or name
                _stream_q.put(("__tool_progress__", {
                    "tool": name,
                    "emoji": emoji,
                    "label": label,
                }))

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                agent_ref=agent_ref,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        return web.json_response(response_data, headers={"X-Sinoclaw-Session-Id": session_id})

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Sinoclaw-Session-Id"] = session_id
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            last_activity = time.monotonic()

            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
            last_activity = time.monotonic()

            # Helper — route a queue item to the correct SSE event.
            async def _emit(item):
                """Write a single queue item to the SSE stream.

                Plain strings are sent as normal ``delta.content`` chunks.
                Tagged tuples ``("__tool_progress__", payload)`` are sent
                as a custom ``event: sinoclaw.tool.progress`` SSE event so
                frontends can display them without storing the markers in
                conversation history.  See #6972.
                """
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__tool_progress__":
                    event_data = json.dumps(item[1])
                    await response.write(
                        f"event: sinoclaw.tool.progress\ndata: {event_data}\n\n".encode()
                    )
                else:
                    content_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": item}, "finish_reason": None}],
                    }
                    await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                return time.monotonic()

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_running_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # End of stream sentinel
                    break

                last_activity = await _emit(delta)

            # Get usage from completed agent
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception:
                pass

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected mid-stream.  Interrupt the agent so it
            # stops making LLM API calls at the next loop iteration, then
            # cancel the asyncio task wrapper.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)

        return response

    async def _write_sse_responses(
        self,
        request: "web.Request",
        response_id: str,
        model: str,
        created_at: int,
        stream_q,
        agent_task,
        agent_ref,
        conversation_history: List[Dict[str, str]],
        user_message: str,
        instructions: Optional[str],
        conversation: Optional[str],
        store: bool,
        session_id: str,
    ) -> "web.StreamResponse":
        """Write an SSE stream for POST /v1/responses (OpenAI Responses API).

        Emits spec-compliant event types as the agent runs:

        - ``response.created`` — initial envelope (status=in_progress)
        - ``response.output_text.delta`` / ``response.output_text.done`` —
          streamed assistant text
        - ``response.output_item.added`` / ``response.output_item.done``
          with ``item.type == "function_call"`` — when the agent invokes a
          tool (both events fire; the ``done`` event carries the finalized
          ``arguments`` string)
        - ``response.output_item.added`` with
          ``item.type == "function_call_output"`` — tool result with
          ``{call_id, output, status}``
        - ``response.completed`` — terminal event carrying the full
          response object with all output items + usage (same payload
          shape as the non-streaming path for parity)
        - ``response.failed`` — terminal event on agent error

        If the client disconnects mid-stream, ``agent.interrupt()`` is
        called so the agent stops issuing upstream LLM calls, then the
        asyncio task is cancelled.  When ``store=True`` the full response
        is persisted to the ResponseStore in a ``finally`` block so GET
        /v1/responses/{id} and ``previous_response_id`` chaining work the
        same as the batch path.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Sinoclaw-Session-Id"] = session_id
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # State accumulated during the stream
        final_text_parts: List[str] = []
        # Track open function_call items by name so we can emit a matching
        # ``done`` event when the tool completes.  Order preserved.
        pending_tool_calls: List[Dict[str, Any]] = []
        # Output items we've emitted so far (used to build the terminal
        # response.completed payload).  Kept in the order they appeared.
        emitted_items: List[Dict[str, Any]] = []
        # Monotonic counter for output_index (spec requires it).
        output_index = 0
        # Monotonic counter for call_id generation if the agent doesn't
        # provide one (it doesn't, from tool_progress_callback).
        call_counter = 0
        # Canonical Responses SSE events include a monotonically increasing
        # sequence_number. Add it server-side for every emitted event so
        # clients that validate the OpenAI event schema can parse our stream.
        sequence_number = 0
        # Track the assistant message item id + content index for text
        # delta events — the spec ties deltas to a specific item.
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_output_index: Optional[int] = None
        message_opened = False

        async def _write_event(event_type: str, data: Dict[str, Any]) -> None:
            nonlocal sequence_number
            if "sequence_number" not in data:
                data["sequence_number"] = sequence_number
            sequence_number += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(payload.encode())

        def _envelope(status: str) -> Dict[str, Any]:
            env: Dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "status": status,
                "created_at": created_at,
                "model": model,
            }
            return env

        final_response_text = ""
        agent_error: Optional[str] = None
        usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        try:
            # response.created — initial envelope, status=in_progress
            created_env = _envelope("in_progress")
            created_env["output"] = []
            await _write_event("response.created", {
                "type": "response.created",
                "response": created_env,
            })
            last_activity = time.monotonic()

            async def _open_message_item() -> None:
                """Emit response.output_item.added for the assistant message
                the first time any text delta arrives."""
                nonlocal message_opened, message_output_index, output_index
                if message_opened:
                    return
                message_opened = True
                message_output_index = output_index
                output_index += 1
                item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": message_output_index,
                    "item": item,
                })

            async def _emit_text_delta(delta_text: str) -> None:
                await _open_message_item()
                final_text_parts.append(delta_text)
                await _write_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "delta": delta_text,
                    "logprobs": [],
                })

            async def _emit_tool_started(payload: Dict[str, Any]) -> str:
                """Emit response.output_item.added for a function_call.

                Returns the call_id so the matching completion event can
                reference it.  Prefer the real ``tool_call_id`` from the
                agent when available; fall back to a generated call id for
                safety in tests or older code paths.
                """
                nonlocal output_index, call_counter
                call_counter += 1
                call_id = payload.get("tool_call_id") or f"call_{response_id[5:]}_{call_counter}"
                args = payload.get("arguments", {})
                if isinstance(args, dict):
                    arguments_str = json.dumps(args)
                else:
                    arguments_str = str(args)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": payload.get("name", ""),
                    "call_id": call_id,
                    "arguments": arguments_str,
                }
                idx = output_index
                output_index += 1
                pending_tool_calls.append({
                    "call_id": call_id,
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "item_id": item["id"],
                    "output_index": idx,
                })
                emitted_items.append({
                    "type": "function_call",
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "call_id": call_id,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                })
                return call_id

            async def _emit_tool_completed(payload: Dict[str, Any]) -> None:
                """Emit response.output_item.done (function_call) followed
                by response.output_item.added (function_call_output)."""
                nonlocal output_index
                call_id = payload.get("tool_call_id")
                result = payload.get("result", "")
                pending = None
                if call_id:
                    for i, p in enumerate(pending_tool_calls):
                        if p["call_id"] == call_id:
                            pending = pending_tool_calls.pop(i)
                            break
                if pending is None:
                    # Completion without a matching start — skip to avoid
                    # emitting orphaned done events.
                    return

                # function_call done
                done_item = {
                    "id": pending["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": pending["name"],
                    "call_id": pending["call_id"],
                    "arguments": pending["arguments"],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": pending["output_index"],
                    "item": done_item,
                })

                # function_call_output added (result)
                result_str = result if isinstance(result, str) else json.dumps(result)
                output_parts = [{"type": "input_text", "text": result_str}]
                output_item = {
                    "id": f"fco_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                    "status": "completed",
                }
                idx = output_index
                output_index += 1
                emitted_items.append({
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": output_item,
                })
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": output_item,
                })

            # Main drain loop — thread-safe queue fed by agent callbacks.
            async def _dispatch(it) -> None:
                """Route a queue item to the correct SSE emitter.

                Plain strings are text deltas.  Tagged tuples with
                ``__tool_started__`` / ``__tool_completed__`` prefixes
                are tool lifecycle events.
                """
                if isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str):
                    tag, payload = it
                    if tag == "__tool_started__":
                        await _emit_tool_started(payload)
                    elif tag == "__tool_completed__":
                        await _emit_tool_completed(payload)
                    # Unknown tags are silently ignored (forward-compat).
                elif isinstance(it, str):
                    await _emit_text_delta(it)
                # Other types (non-string, non-tuple) are silently dropped.

            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain remaining
                        while True:
                            try:
                                item = stream_q.get_nowait()
                                if item is None:
                                    break
                                await _dispatch(item)
                                last_activity = time.monotonic()
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if item is None:  # EOS sentinel
                    break

                await _dispatch(item)
                last_activity = time.monotonic()

            # Pick up agent result + usage from the completed task
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                # If the agent produced a final_response but no text
                # deltas were streamed (e.g. some providers only emit
                # the full response at the end), emit a single fallback
                # delta so Responses clients still receive a live text part.
                agent_final = result.get("final_response", "") if isinstance(result, dict) else ""
                if agent_final and not final_text_parts:
                    await _emit_text_delta(agent_final)
                if agent_final and not final_response_text:
                    final_response_text = agent_final
                if isinstance(result, dict) and result.get("error") and not final_response_text:
                    agent_error = result["error"]
            except Exception as e:  # noqa: BLE001
                logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
                agent_error = str(e)

            # Close the message item if it was opened
            final_response_text = "".join(final_text_parts) or final_response_text
            if message_opened:
                await _write_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": final_response_text,
                    "logprobs": [],
                })
                msg_done_item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": final_response_text}
                    ],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": message_output_index,
                    "item": msg_done_item,
                })

            # Always append a final message item in the completed
            # response envelope so clients that only parse the terminal
            # payload still see the assistant text.  This mirrors the
            # shape produced by _extract_output_items in the batch path.
            final_items: List[Dict[str, Any]] = list(emitted_items)
            final_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": final_response_text or (agent_error or "")}
                ],
            })

            if agent_error:
                failed_env = _envelope("failed")
                failed_env["output"] = final_items
                failed_env["error"] = {"message": agent_error, "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            else:
                completed_env = _envelope("completed")
                completed_env["output"] = final_items
                completed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                await _write_event("response.completed", {
                    "type": "response.completed",
                    "response": completed_env,
                })

                # Persist for future chaining / GET retrieval, mirroring
                # the batch path behavior.
                if store:
                    full_history = list(conversation_history)
                    full_history.append({"role": "user", "content": user_message})
                    if isinstance(result, dict) and result.get("messages"):
                        full_history.extend(result["messages"])
                    else:
                        full_history.append({"role": "assistant", "content": final_response_text})
                    self._response_store.put(response_id, {
                        "response": completed_env,
                        "conversation_history": full_history,
                        "instructions": instructions,
                        "session_id": session_id,
                    })
                    if conversation:
                        self._response_store.set_conversation(conversation, response_id)

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected — interrupt the agent so it stops
            # making upstream LLM calls, then cancel the task.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)

        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = body.get("store", True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, str]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    content = _normalize_chat_content(item.get("content", ""))
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Accept explicit conversation_history from the request body.
        # This lets stateless clients supply their own history instead of
        # relying on server-side response chaining via previous_response_id.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message = input_messages[-1].get("content", "") if input_messages else ""
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # Reuse session from previous_response_id chain so the dashboard
        # groups the entire conversation under one session entry.
        session_id = stored_session_id or str(uuid.uuid4())

        stream = bool(body.get("stream", False))
        if stream:
            # Streaming branch — emit OpenAI Responses SSE events as the
            # agent runs so frontends can render text deltas and tool
            # calls in real time.  See _write_sse_responses for details.
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # None from the agent is a CLI box-close signal, not EOS.
                # Forwarding would kill the SSE stream prematurely; the
                # SSE writer detects completion via agent_task.done().
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Queue non-start tool progress events if needed in future.

                The structured Responses stream uses ``tool_start_callback``
                and ``tool_complete_callback`` for exact call-id correlation,
                so progress events are currently ignored here.
                """
                return

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Queue a started tool for live function_call streaming."""
                _stream_q.put(("__tool_started__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Queue a completed tool result for live function_call_output streaming."""
                _stream_q.put(("__tool_completed__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                    "result": function_result,
                }))

            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
            ))

            response_id = f"resp_{uuid.uuid4().hex[:28]}"
            model_name = body.get("model", self._model_name)
            created_at = int(time.time())

            return await self._write_sse_responses(
                request=request,
                response_id=response_id,
                model=model_name,
                created_at=created_at,
                stream_q=_stream_q,
                agent_task=agent_task,
                agent_ref=agent_ref,
                conversation_history=conversation_history,
                user_message=user_message,
                instructions=instructions,
                conversation=conversation,
                store=store,
                session_id=session_id,
            )

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = list(conversation_history)
        full_history.append({"role": "user", "content": user_message})
        # Add agent's internal messages if available
        agent_messages = result.get("messages", [])
        if agent_messages:
            full_history.extend(agent_messages)
        else:
            full_history.append({"role": "assistant", "content": final_response})

        # Build output items (includes tool calls + final message)
        output_items = self._extract_output_items(result)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", self._model_name),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
                "session_id": session_id,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        return web.json_response(response_data)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    # Check cron module availability once (not per-request)
    _CRON_AVAILABLE = False
    try:
        from cron.jobs import (
            list_jobs as _cron_list,
            get_job as _cron_get,
            create_job as _cron_create,
            update_job as _cron_update,
            remove_job as _cron_remove,
            pause_job as _cron_pause,
            resume_job as _cron_resume,
            trigger_job as _cron_trigger,
        )
        # Wrap as staticmethod to prevent descriptor binding — these are plain
        # module functions, not instance methods.  Without this, self._cron_*()
        # injects ``self`` as the first positional argument and every call
        # raises TypeError.
        _cron_list = staticmethod(_cron_list)
        _cron_get = staticmethod(_cron_get)
        _cron_create = staticmethod(_cron_create)
        _cron_update = staticmethod(_cron_update)
        _cron_remove = staticmethod(_cron_remove)
        _cron_pause = staticmethod(_cron_pause)
        _cron_resume = staticmethod(_cron_resume)
        _cron_trigger = staticmethod(_cron_trigger)
        _CRON_AVAILABLE = True
    except ImportError:
        pass

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    def _check_jobs_available(self) -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not self._CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs — list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in ("true", "1")
            jobs = self._cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs — create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = self._cron_create(**kwargs)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} — get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} — update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            job = self._cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} — delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = self._cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause — pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume — resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run — trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_output_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Build the full output item array from the agent's messages.

        Walks *result["messages"]* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        agent_ref: Optional[list] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        loop = asyncio.get_running_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=stream_delta_callback,
                tool_progress_callback=tool_progress_callback,
                tool_start_callback=tool_start_callback,
                tool_complete_callback=tool_complete_callback,
            )
            if agent_ref is not None:
                agent_ref[0] = agent
            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=conversation_history,
                task_id="default",
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            return result, usage

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _MAX_CONCURRENT_RUNS = 10  # Prevent unbounded resource allocation
    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded

        return _callback

    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Enforce concurrency limit
        if len(self._run_streams) >= self._MAX_CONCURRENT_RUNS:
            return web.json_response(
                _openai_error(f"Too many concurrent runs (max {self._MAX_CONCURRENT_RUNS})", code="rate_limit_exceeded"),
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        run_id = f"run_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = time.time()

        event_cb = self._make_run_event_callback(run_id, loop)

        # Also wire stream_delta_callback so message.delta events flow through
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                stored_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        session_id = body.get("session_id") or stored_session_id or run_id
        ephemeral_system_prompt = instructions

        async def _run_and_close():
            try:
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                )
                def _run_sync():
                    r = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id="default",
                    )
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                q.put_nowait({
                    "event": "run.completed",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "output": final_response,
                    "usage": usage,
                })
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                # Sentinel: signal SSE stream to close
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

        task = asyncio.create_task(_run_and_close())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"run_id": run_id, "status": "started"}, status=202)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished — send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically clean up run streams that were never consumed."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                run_id
                for run_id, created_at in list(self._run_streams_created.items())
                if now - created_at > self._RUN_STREAM_TTL
            ]
            for run_id in stale:
                logger.debug("[api_server] sweeping orphaned run %s", run_id)
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)

    # ── Chat Session Management (Console UI) ──────────────────────────────────


    # Agent workspace files and memory ----------------------------------------
    def _agent_workspace_dir(self, agent_id: str) -> Path:
        """Return workspace directory for an agent."""
        return Path.home() / ".sinoclaw" / "workspaces" / agent_id

    def _memory_dir(self) -> Path:
        """Return memory directory."""
        return Path.home() / ".sinoclaw" / "memories"

    def _system_prompts_file(self) -> Path:
        return Path.home() / ".sinoclaw" / "system_prompts.json"

    def _file_info(self, path: Path) -> dict:
        stat = path.stat()
        return {
            "filename": path.name,
            "path": str(path),
            "size": stat.st_size,
            "created_time": stat.st_ctime,
            "modified_time": stat.st_mtime,
        }

    async def _handle_list_agent_files(self, request: "web.Request") -> "web.Response":
        """GET /agents/{agent_id}/files"""
        agent_id = request.match_info.get("agent_id", "default")
        workspace = self._agent_workspace_dir(agent_id)
        workspace.mkdir(parents=True, exist_ok=True)
        files = []
        for p in sorted(workspace.glob("*.md")):
            files.append(self._file_info(p))
        return web.json_response(files)

    async def _handle_read_agent_file(self, request: "web.Request") -> "web.Response":
        """GET /agents/{agent_id}/files/{filename}"""
        agent_id = request.match_info.get("agent_id", "default")
        filename = request.match_info.get("filename", "")
        workspace = self._agent_workspace_dir(agent_id)
        file_path = workspace / filename
        if not file_path.exists() or not file_path.is_file():
            return web.json_response({"error": "file not found"}, status=404)
        try:
            content = file_path.read_text(encoding="utf-8")
            return web.json_response({"content": content})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_write_agent_file(self, request: "web.Request") -> "web.Response":
        """PUT /agents/{agent_id}/files/{filename}"""
        agent_id = request.match_info.get("agent_id", "default")
        filename = request.match_info.get("filename", "")
        workspace = self._agent_workspace_dir(agent_id)
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            body = await request.json()
            content = body.get("content", "")
            file_path = workspace / filename
            file_path.write_text(content, encoding="utf-8")
            return web.json_response({"written": True, "filename": filename})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_list_agent_memory(self, request: "web.Request") -> "web.Response":
        """GET /agents/{agent_id}/memory"""
        agent_id = request.match_info.get("agent_id", "default")
        memory_dir = self._memory_dir()
        files = []
        if memory_dir.exists():
            for p in sorted(memory_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
                files.append(self._file_info(p))
        return web.json_response(files)

    async def _handle_read_agent_memory(self, request: "web.Request") -> "web.Response":
        """GET /agents/{agent_id}/memory/{date}.md"""
        date = request.match_info.get("date", "")
        memory_dir = self._memory_dir()
        file_path = memory_dir / f"{date}.md"
        if not file_path.exists():
            return web.json_response({"error": "not found"}, status=404)
        try:
            content = file_path.read_text(encoding="utf-8")
            return web.json_response({"content": content})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_write_agent_memory(self, request: "web.Request") -> "web.Response":
        """PUT /agents/{agent_id}/memory/{date}.md"""
        date = request.match_info.get("date", "")
        memory_dir = self._memory_dir()
        memory_dir.mkdir(parents=True, exist_ok=True)
        try:
            body = await request.json()
            content = body.get("content", "")
            file_path = memory_dir / f"{date}.md"
            file_path.write_text(content, encoding="utf-8")
            return web.json_response({"written": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_system_prompt_files(self, request: "web.Request") -> "web.Response":
        """GET /agent/system-prompt-files"""
        f = self._system_prompts_file()
        if f.exists():
            import json as _json
            return web.json_response(_json.loads(f.read_text(encoding="utf-8")))
        return web.json_response([])

    async def _handle_set_system_prompt_files(self, request: "web.Request") -> "web.Response":
        """PUT /agent/system-prompt-files"""
        try:
            body = await request.json()
            import json as _json
            self._system_prompts_file().write_text(_json.dumps(body, indent=2), encoding="utf-8")
            return web.json_response(body)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_workspace_download(self, request: "web.Request") -> "web.Response":
        """GET /workspace/download"""
        import io, zipfile, datetime
        workspace = Path.home() / ".sinoclaw"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(workspace.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    zf.write(p, p.relative_to(workspace.parent))
        buf.seek(0)
        from aiohttp import web
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"sinoclaw_workspace_{timestamp}.zip"
        return web.Response(
            body=buf.getvalue(),
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-cache",
            },
        )

    async def _handle_workspace_upload(self, request: "web.Request") -> "web.Response":
        """POST /workspace/upload"""
        import io, zipfile, shutil
        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response({"success": False, "message": "no file"}, status=400)
            data = b""
            async for chunk in field:
                data += chunk
            buf = io.BytesIO(data)
            if not zipfile.is_zipfile(buf):
                return web.json_response({"success": False, "message": "not a zip"}, status=400)
            buf.seek(0)
            workspace = Path.home() / ".sinoclaw"
            with zipfile.ZipFile(buf, "r") as zf:
                for name in zf.namelist():
                    # security: prevent path traversal
                    safe_name = name.lstrip("/")
                    dest = (workspace / safe_name).resolve()
                    if not str(dest).startswith(str(workspace.resolve())):
                        continue
                    if name.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(name))
            return web.json_response({"success": True, "message": "uploaded"})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    # Built-in tools registry (static list for Console UI) -------------------
    BUILTIN_TOOLS = [
        {"name": "bash", "description": "Execute shell commands", "icon": "Terminal", "async_execution": False},
        {"name": "read", "description": "Read file contents", "icon": "FileText", "async_execution": False},
        {"name": "write", "description": "Write/edit files", "icon": "Edit", "async_execution": False},
        {"name": "glob", "description": "Find files by pattern", "icon": "FolderOpen", "async_execution": False},
        {"name": "grep", "description": "Search file contents", "icon": "Search", "async_execution": False},
        {"name": "memory", "description": "Memory and recall", "icon": "Brain", "async_execution": False},
        {"name": "skill", "description": "Invoke a skill", "icon": "Tool", "async_execution": False},
        {"name": "web_fetch", "description": "Fetch web page content", "icon": "Globe", "async_execution": False},
        {"name": "image_generate", "description": "Generate images", "icon": "Image", "async_execution": False},
        {"name": "tts", "description": "Text-to-speech", "icon": "Audio", "async_execution": False},
        {"name": "music_generate", "description": "Generate music", "icon": "Music", "async_execution": False},
        {"name": "video_generate", "description": "Generate videos", "icon": "Video", "async_execution": False},
        {"name": "pdf", "description": "Analyze PDF documents", "icon": "FilePdf", "async_execution": False},
        {"name": "process", "description": "Manage background processes", "icon": "Process", "async_execution": True},
        {"name": "send_message", "description": "Send messages to channels", "icon": "Send", "async_execution": True},
        {"name": "cronjob", "description": "Schedule cron jobs", "icon": "Clock", "async_execution": False},
        {"name": "code_execution", "description": "Run code in sandbox", "icon": "Code", "async_execution": True},
    ]

    def _tools_state_file(self) -> Path:
        return Path.home() / ".sinoclaw" / "tools_state.json"

    def _load_tools_state(self) -> dict:
        f = self._tools_state_file()
        if f.exists():
            import json as _json
            return _json.loads(f.read_text(encoding="utf-8"))
        return {}

    def _save_tools_state(self, state: dict) -> None:
        import json as _json
        f = self._tools_state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(state, indent=2), encoding="utf-8")

    def _get_tool_info(self, tool_name: str) -> dict:
        state = self._load_tools_state()
        for t in self.BUILTIN_TOOLS:
            if t["name"] == tool_name:
                result = t.copy()
                result["enabled"] = state.get(tool_name, {}).get("enabled", True)
                result["async_execution"] = state.get(tool_name, {}).get("async_execution", t.get("async_execution", False))
                return result
        return {"name": tool_name, "description": "", "enabled": True, "async_execution": False, "icon": "Tool"}

    async def _handle_list_tools(self, request: "web.Request") -> "web.Response":
        """GET /tools"""
        state = self._load_tools_state()
        result = []
        for t in self.BUILTIN_TOOLS:
            tool_state = state.get(t["name"], {})
            result.append({
                **t,
                "enabled": tool_state.get("enabled", True),
                "async_execution": tool_state.get("async_execution", t.get("async_execution", False)),
            })
        return web.json_response(result)

    async def _handle_toggle_tool(self, request: "web.Request") -> "web.Response":
        """PATCH /tools/{tool_name}/toggle"""
        tool_name = request.match_info.get("tool_name", "")
        state = self._load_tools_state()
        if tool_name not in state:
            state[tool_name] = {}
        current = state[tool_name].get("enabled", True)
        state[tool_name]["enabled"] = not current
        self._save_tools_state(state)
        return web.json_response(self._get_tool_info(tool_name))

    async def _handle_tool_async_execution(self, request: "web.Request") -> "web.Response":
        """PUT /tools/{tool_name}/async-execution"""
        tool_name = request.match_info.get("tool_name", "")
        try:
            body = await request.json()
            state = self._load_tools_state()
            if tool_name not in state:
                state[tool_name] = {}
            state[tool_name]["async_execution"] = bool(body.get("async_execution", False))
            self._save_tools_state(state)
            return web.json_response(self._get_tool_info(tool_name))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # Token usage ------------------------------------------------------------
    async def _handle_token_usage(self, request: "web.Request") -> "web.Response":
        """GET /token-usage"""
        import datetime
        start_date = request.query.get("start_date", "")
        end_date = request.query.get("end_date", "")
        # Return zeros since we don't track per-request token usage
        return web.json_response({
            "total_tokens": 0,
            "total_cost": "0.00",
            "start_date": start_date,
            "end_date": end_date,
            "daily": [],
        })

    # Security config stubs --------------------------------------------------
    # Security: tool guard builtin rules
    BUILTIN_TOOL_GUARD_RULES = [
        {"id": "shell_cmd", "tools": ["bash", "exec", "run"], "params": [], "category": "shell",
         "severity": "high", "patterns": ["rm -rf", "drop table", "delete from"], "exclude_patterns": [],
         "description": "Guard dangerous shell commands", "remediation": "Review command before execution"},
        {"id": "file_write", "tools": ["write", "write_file", "edit"], "params": [], "category": "filesystem",
         "severity": "medium", "patterns": [], "exclude_patterns": [],
         "description": "Monitor file writes", "remediation": "Confirm file path is correct"},
        {"id": "network_request", "tools": ["web_fetch", "http_request"], "params": [], "category": "network",
         "severity": "medium", "patterns": [], "exclude_patterns": [],
         "description": "Monitor network requests", "remediation": "Verify URL is trusted"},
    ]

    def _security_config_file(self) -> Path:
        return Path.home() / ".sinoclaw" / "security.json"

    def _load_security_config(self) -> dict:
        f = self._security_config_file()
        if f.exists():
            import json as _json
            return _json.loads(f.read_text(encoding="utf-8"))
        return {
            "tool_guard": {"enabled": False, "guarded_tools": None, "denied_tools": [], "custom_rules": [], "disabled_rules": []},
            "file_guard": {"enabled": False, "paths": []},
            "skill_scanner": {"mode": "off", "timeout": 30, "whitelist": []},
            "blocked_history": [],
        }

    def _save_security_config(self, cfg: dict) -> None:
        import json as _json
        f = self._security_config_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")

    async def _handle_tool_guard_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT /config/security/tool-guard"""
        cfg = self._load_security_config()
        if request.method == "PUT":
            try:
                body = await request.json()
                cfg["tool_guard"] = body
                self._save_security_config(cfg)
                return web.json_response(body)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response(cfg.get("tool_guard", {
            "enabled": False, "guarded_tools": None, "denied_tools": [],
            "custom_rules": [], "disabled_rules": [],
        }))

    async def _handle_tool_guard_builtin_rules(self, request: "web.Request") -> "web.Response":
        """GET /config/security/tool-guard/builtin-rules"""
        return web.json_response(self.BUILTIN_TOOL_GUARD_RULES)

    async def _handle_file_guard_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT /config/security/file-guard"""
        cfg = self._load_security_config()
        if request.method == "PUT":
            try:
                body = await request.json()
                cfg["file_guard"] = body
                self._save_security_config(cfg)
                return web.json_response(body)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response(cfg.get("file_guard", {"enabled": False, "paths": []}))

    async def _handle_skill_scanner_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT /config/security/skill-scanner"""
        cfg = self._load_security_config()
        if request.method == "PUT":
            try:
                body = await request.json()
                cfg["skill_scanner"] = body
                self._save_security_config(cfg)
                return web.json_response(body)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response(cfg.get("skill_scanner", {"mode": "off", "timeout": 30, "whitelist": []}))

    async def _handle_skill_scanner_blocked_history(self, request: "web.Request") -> "web.Response":
        """GET/DELETE /config/security/skill-scanner/blocked-history"""
        cfg = self._load_security_config()
        if request.method == "DELETE":
            cfg["blocked_history"] = []
            self._save_security_config(cfg)
            return web.json_response({"cleared": True})
        return web.json_response(cfg.get("blocked_history", []))

    async def _handle_skill_scanner_blocked_history_delete(self, request: "web.Request") -> "web.Response":
        """DELETE /config/security/skill-scanner/blocked-history/{index}"""
        idx = int(request.match_info.get("index", 0))
        cfg = self._load_security_config()
        if 0 <= idx < len(cfg.get("blocked_history", [])):
            cfg["blocked_history"].pop(idx)
            self._save_security_config(cfg)
        return web.json_response({"removed": True})

    async def _handle_skill_scanner_whitelist_add(self, request: "web.Request") -> "web.Response":
        """POST /config/security/skill-scanner/whitelist"""
        try:
            body = await request.json()
            skill_name = body.get("skill_name", "")
            cfg = self._load_security_config()
            wl = cfg.get("skill_scanner", {}).get("whitelist", [])
            if not any(e.get("skill_name") == skill_name for e in wl):
                wl.append({"skill_name": skill_name, "content_hash": body.get("content_hash", ""), "added_at": ""})
                cfg["skill_scanner"]["whitelist"] = wl
                self._save_security_config(cfg)
            return web.json_response({"whitelisted": True, "skill_name": skill_name})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_skill_scanner_whitelist_remove(self, request: "web.Request") -> "web.Response":
        """DELETE /config/security/skill-scanner/whitelist/{skill_name}"""
        skill_name = request.match_info.get("skill_name", "")
        cfg = self._load_security_config()
        wl = cfg.get("skill_scanner", {}).get("whitelist", [])
        cfg["skill_scanner"]["whitelist"] = [e for e in wl if e.get("skill_name") != skill_name]
        self._save_security_config(cfg)
        return web.json_response({"removed": True, "skill_name": skill_name})
        """GET/PUT /config/security/file-guard"""
        return web.json_response({"enabled": False, "paths": []})

    async def _handle_skill_scanner_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT /config/security/skill-scanner"""
        return web.json_response({"enabled": False, "blocked_history": [], "whitelist": []})

    # Config channels stub ---------------------------------------------------
    def _load_channel_config(self) -> dict:
        """Build a full ChannelConfig object from config.yaml and .env."""
        import os as _os, yaml
        config_file = Path.home() / ".sinoclaw" / "config.yaml"
        cfg = {}
        if config_file.exists():
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
        # Read .env for channel credentials
        env_file = Path.home() / ".sinoclaw" / ".env"
        env_vars = {}
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
        # Build full channel config (all channel types, even if not enabled)
        enabled = cfg.get("weixin", {}).get("enabled", True)
        wx_account = env_vars.get("WEIXIN_ACCOUNT_ID", "")
        result = {
            "console": {"enabled": True, "bot_prefix": ""},
            "weixin": {
                "enabled": enabled,
                "account_id": wx_account,
                "bot_prefix": "",
            },
            "qq": {
                "enabled": False,
                "app_id": env_vars.get("QQ_APP_ID", ""),
                "client_secret": env_vars.get("QQ_CLIENT_SECRET", ""),
                "ack_message": "",
            },
            "feishu": {
                "enabled": False,
                "app_id": "",
                "app_secret": "",
                "encrypt_key": "",
                "verification_token": "",
                "media_dir": "",
            },
            "dingtalk": {
                "enabled": False,
                "client_id": "",
                "client_secret": "",
                "message_type": "",
                "card_template_id": "",
                "card_template_key": "",
                "robot_code": "",
            },
            "telegram": {
                "enabled": False,
                "bot_token": env_vars.get("TELEGRAM_BOT_TOKEN", ""),
                "http_proxy": "",
                "http_proxy_auth": "",
                "show_typing": False,
            },
            "discord": {
                "enabled": False,
                "bot_token": "",
                "http_proxy": "",
                "http_proxy_auth": "",
                "accept_bot_messages": False,
            },
            "wecom": {
                "enabled": False,
                "bot_id": "",
                "secret": "",
            },
            "xiaoyi": {"enabled": False, "ak": "", "sk": "", "agent_id": ""},
            "matrix": {"enabled": False, "homeserver": "", "user_id": "", "access_token": ""},
            "imessage": {"enabled": False, "db_path": "", "poll_sec": 5},
            "onebot": {"enabled": False, "ws_host": "", "ws_port": 3000, "access_token": "", "share_session_in_group": False},
        }
        return result

    def _save_channel_config(self, channels: dict) -> None:
        """Persist channel config changes to config.yaml and .env."""
        import os as _os, yaml
        config_file = Path.home() / ".sinoclaw" / "config.yaml"
        cfg = {}
        if config_file.exists():
            with open(config_file) as f:
                cfg = yaml.safe_load(f) or {}
        # Update weixin enabled
        if "weixin" in channels:
            cfg["weixin"] = cfg.get("weixin", {})
            cfg["weixin"]["enabled"] = channels["weixin"].get("enabled", True)
        # Save credentials to .env
        env_file = Path.home() / ".sinoclaw" / ".env"
        env_vars = {}
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
        # Map channel creds to env vars
        if channels.get("qq", {}).get("app_id"):
            env_vars["QQ_APP_ID"] = channels["qq"]["app_id"]
        if channels.get("qq", {}).get("client_secret"):
            env_vars["QQ_CLIENT_SECRET"] = channels["qq"]["client_secret"]
        if channels.get("telegram", {}).get("bot_token"):
            env_vars["TELEGRAM_BOT_TOKEN"] = channels["telegram"]["bot_token"]
        # Write .env
        lines = [f"{k}={v}" for k, v in env_vars.items()]
        env_file.write_text("\n".join(lines) + "\n")
        # Write config.yaml
        cfg["platforms"] = cfg.get("platforms", {})
        with open(config_file, "w") as f:
            yaml.dump(cfg, f)

    async def _handle_config_channels(self, request: "web.Request") -> "web.Response":
        """GET /config/channels"""
        if request.method == "PUT":
            try:
                body = await request.json()
                self._save_channel_config(body)
                return web.json_response(body)
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response(self._load_channel_config())

    async def _handle_config_channel_types(self, request: "web.Request") -> "web.Response":
        """GET /config/channels/types"""
        return web.json_response([
            "console", "weixin", "dingtalk", "feishu",
            "telegram", "discord", "qq", "wecom", "xiaoyi", "matrix", "imessage", "onebot",
        ])

    async def _handle_config_channel(self, request: "web.Request") -> "web.Response":
        """GET/PUT /config/channels/{channel}"""
        channel = request.match_info.get("channel", "")
        cfg = self._load_channel_config()
        if channel not in cfg:
            return web.json_response({"error": "channel not found"}, status=404)
        if request.method == "PUT":
            try:
                body = await request.json()
                cfg[channel] = body
                self._save_channel_config(cfg)
                return web.json_response(cfg[channel])
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response(cfg.get(channel, {}))

    async def _handle_config_channel_qrcode(self, request: "web.Request") -> "web.Response":
        """GET /config/channels/{channel}/qrcode"""
        channel = request.match_info.get("channel", "")
        # WeChat doesn't use QR code login - return not supported
        return web.json_response({
            "qrcode_img": "",
            "poll_token": "",
            "error": "QR code not supported for this channel",
        }, status=501)

    async def _handle_config_channel_qrcode_status(self, request: "web.Request") -> "web.Response":
        """GET /config/channels/{channel}/qrcode/status"""
        channel = request.match_info.get("channel", "")
        return web.json_response({
            "status": "not_supported",
            "credentials": {},
        })

    async def _handle_list_chats(self, request: "web.Request") -> "web.Response":
        """GET /chats — List all chat sessions, most recent first."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"error": "Session database not available"}, status=500)

        try:
            # Support filtering by user_id and channel via query params
            user_id = request.query.get("user_id")
            channel = request.query.get("channel")
            limit = int(request.query.get("limit", 50))
            offset = int(request.query.get("offset", 0))

            # Build exclude list: exclude all non-console/non-api sources unless specified
            exclude_sources = []
            if not channel and not user_id:
                # Default: only show console sessions
                exclude_sources = ["api", "slack", "telegram", "discord", "feishu", "wecom", "weixin", "whatsapp", "signal", "sms", "email", "dingtalk", "matrix", "mattermost", "bluebubbles", "zalo", "zalouser", "nostr", "msteams", "nextcloud-talk", "webhook"]

            sessions = db.list_sessions_rich(
                source=channel or None,
                exclude_sources=exclude_sources if not channel else None,
                limit=limit,
                offset=offset,
            )

            # Format for console
            result = []
            for s in sessions:
                result.append({
                    "id": s["id"],
                    "name": s.get("title") or s.get("id", "")[:8],
                    "session_id": s["id"],
                    "user_id": user_id or "default",
                    "channel": channel or s.get("source", "console"),
                    "status": "idle",
                    "pinned": False,
                    "created_at": s.get("started_at"),
                    "updated_at": s.get("last_active"),
                    "message_count": s.get("message_count", 0),
                    "preview": s.get("preview", ""),
                })

            return web.json_response(result)
        except Exception as e:
            logger.exception("[api_server] _handle_list_chats failed")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_chat(self, request: "web.Request") -> "web.Response":
        """POST /chats — Create a new chat session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"error": "Session database not available"}, status=500)

        try:
            body = await request.json()
        except Exception:
            body = {}

        user_id = body.get("user_id", "default")
        channel = body.get("channel", "console")
        name = body.get("name") or body.get("title")

        # Generate session_id like SessionStore does: timestamp_uuid8
        import time as _time, uuid as _uuid
        session_id = f"{_time.strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:8]}"
        db.create_session(session_id=session_id, source=channel, user_id=user_id)
        if name:
            db.set_session_title(session_id, name)

        return web.json_response({
            "id": session_id,
            "name": name or session_id[:8],
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel,
            "status": "idle",
            "pinned": False,
            "created_at": None,
        }, status=201)
    async def _handle_get_chat(self, request: "web.Request") -> "web.Response":
        """GET /chats/{chat_id} — Get chat history and metadata."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        chat_id = request.match_info.get("chat_id", "")
        if not chat_id:
            return web.json_response({"error": "Missing chat_id"}, status=400)

        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"error": "Session database not available"}, status=500)

        try:
            session = db.get_session(chat_id)
            if not session:
                return web.json_response({"error": "Chat not found"}, status=404)

            messages = db.get_messages_as_conversation(chat_id)
            title = db.get_session_title(chat_id)

            return web.json_response({
                "id": chat_id,
                "name": title or chat_id[:8],
                "session_id": chat_id,
                "user_id": session.get("user_id", "default"),
                "channel": session.get("source", "console"),
                "status": "idle",
                "pinned": False,
                "created_at": session.get("started_at"),
                "messages": messages,
            })
        except Exception as e:
            logger.exception("[api_server] _handle_get_chat failed")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_chat(self, request: "web.Request") -> "web.Response":
        """PUT /chats/{chat_id} — Update chat metadata (title, pinned)."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        chat_id = request.match_info.get("chat_id", "")
        if not chat_id:
            return web.json_response({"error": "Missing chat_id"}, status=400)

        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"error": "Session database not available"}, status=500)

        try:
            body = await request.json()
        except Exception:
            body = {}

        name = body.get("name") or body.get("title")
        if name:
            db.set_session_title(chat_id, name)

        return web.json_response({"id": chat_id, "name": name or chat_id[:8]})
    async def _handle_delete_chat(self, request: "web.Request") -> "web.Response":
        """DELETE /chats/{chat_id} — Delete a chat session."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        chat_id = request.match_info.get("chat_id", "")
        if not chat_id:
            return web.json_response({"error": "Missing chat_id"}, status=400)

        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"error": "Session database not available"}, status=500)

        try:
            db.delete_session(chat_id)
            return web.json_response({"success": True, "deleted_count": 1})
        except Exception as e:
            logger.exception("[api_server] _handle_delete_chat failed")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_batch_delete_chats(self, request: "web.Request") -> "web.Response":
        """POST /chats/batch-delete — Delete multiple chat sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response({"error": "Session database not available"}, status=500)

        try:
            body = await request.json()
            chat_ids = body if isinstance(body, list) else body.get("chat_ids", [])
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        deleted = 0
        for chat_id in chat_ids:
            if db.delete_session(chat_id):
                deleted += 1

        return web.json_response({"success": True, "deleted_count": deleted})


    # ------------------------------------------------------------------
    # ── Console stub handlers (prevent 404 errors) ─────────────────────────────

    async def _handle_console_chat_stop(self, request: "web.Request") -> "web.Response":
        """POST /console/chat/stop — Stop ongoing generation."""
        return web.json_response({"success": True, "message": "stopped"})

    async def _handle_console_upload(self, request: "web.Request") -> "web.Response":
        """POST /console/upload — File upload."""
        try:
            import uuid as _uuid, os as _os
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response({"error": "no file provided"}, status=400)
            filename = field.name or "upload"
            original_name = getattr(field, "filename", filename)
            # Generate unique stored name
            ext = _os.path.splitext(original_name)[1]
            stored_name = f"{_uuid.uuid4().hex[:16]}{ext}"
            upload_dir = Path.home() / ".sinoclaw" / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            file_path = upload_dir / stored_name
            with open(file_path, "wb") as f:
                async for chunk in field:
                    f.write(chunk)
            url = f"/files/preview/{stored_name}"
            return web.json_response({
                "url": url,
                "file_name": original_name,
                "stored_name": stored_name,
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_files_preview(self, request: "web.Request") -> "web.Response":
        """GET /files/preview/{name} — Serve uploaded file."""
        name = request.match_info.get("name", "")
        if not name:
            return web.Response(status=404)
        upload_dir = Path.home() / ".sinoclaw" / "uploads"
        file_path = upload_dir / name
        if not file_path.exists():
            return web.json_response({"error": "file not found"}, status=404)
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(file_path))
        return web.FileResponse(file_path, headers={"Cache-Control": "max-age=86400"})

    async def _handle_list_agents(self, request: "web.Request") -> "web.Response":
        """GET /agents — List agents (single-agent mode, returns default)."""
        return web.json_response({
            "agents": [{
                "id": "default",
                "name": "Sinoclaw Agent",
                "description": "Default Sinoclaw AI agent",
                "workspace_dir": "/data/sinoclaw",
                "enabled": True,
            }],
            "agent_ids": ["default"]
        })

    async def _handle_get_agent(self, request: "web.Request") -> "web.Response":
        """GET /agents/{agent_id}"""
        agent_id = request.match_info.get("agent_id", "default")
        if agent_id == "default":
            return web.json_response({
                "id": "default",
                "name": "Sinoclaw Agent",
                "description": "Default Sinoclaw AI agent",
                "workspace_dir": "/data/sinoclaw",
                "channels": {},
                "mcp": {},
                "heartbeat": {},
                "running": {},
                "llm_routing": {},
                "system_prompt_files": [],
                "tools": {},
                "security": {},
            })
        return web.json_response({"error": "agent not found"}, status=404)

    async def _handle_create_agent(self, request: "web.Request") -> "web.Response":
        """POST /agents"""
        return web.json_response({"error": "not implemented"}, status=501)

    async def _handle_update_agent(self, request: "web.Request") -> "web.Response":
        """PUT /agents/{id}"""
        return web.json_response({"error": "not implemented"}, status=501)

    async def _handle_delete_agent(self, request: "web.Request") -> "web.Response":
        """DELETE /agents/{id}"""
        return web.json_response({"error": "not implemented"}, status=501)

    async def _handle_reorder_agents(self, request: "web.Request") -> "web.Response":
        """PUT /agents/order"""
        return web.json_response({"error": "not implemented"}, status=501)

    async def _handle_toggle_agent(self, request: "web.Request") -> "web.Response":
        """PATCH /agents/{id}/toggle"""
        return web.json_response({"error": "not implemented"}, status=501)

    async def _handle_agent_files(self, request: "web.Request") -> "web.Response":
        """GET /agents/{id}/files"""
        return web.json_response([])

    async def _handle_agent_memory(self, request: "web.Request") -> "web.Response":
        """GET /agents/{id}/memory"""
        return web.json_response([])

    async def _handle_list_channels(self, request: "web.Request") -> "web.Response":
        """GET /channels — List messaging channels."""
        # WeChat is hardcoded as we know it's configured
        return web.json_response([
            {"id": "weixin", "name": "WeChat", "type": "weixin",
             "enabled": True, "status": "connected", "account": "5947ba72"},
        ])

    async def _handle_get_channel(self, request: "web.Request") -> "web.Response":
        """GET /channels/{channel_id}"""
        channel_id = request.match_info.get("channel_id", "")
        if channel_id == "weixin":
            return web.json_response({"id": "weixin", "name": "WeChat", "type": "weixin",
                                     "enabled": True, "status": "connected", "account": "5947ba72"})
        return web.json_response({"error": "channel not found"}, status=404)

    async def _handle_list_cronjobs(self, request: "web.Request") -> "web.Response":
        """GET /cronjobs — List cron jobs."""
        if not self._CRON_AVAILABLE:
            return web.json_response([])
        try:
            jobs = self._cron_list(include_disabled=False)
            return web.json_response(jobs)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_cronjob(self, request: "web.Request") -> "web.Response":
        """GET /cronjobs/{job_id}"""
        if not self._CRON_AVAILABLE:
            return web.json_response({"error": "Cron not available"}, status=501)
        job_id = request.match_info.get("job_id", "")
        try:
            job = self._cron_get(job_id)
            if job:
                return web.json_response(job)
            return web.json_response({"error": "Job not found"}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_cronjob(self, request: "web.Request") -> "web.Response":
        """POST /cronjobs"""
        if not self._CRON_AVAILABLE:
            return web.json_response({"error": "Cron not available"}, status=501)
        try:
            body = await request.json()
            # Adapt CronJobSpecInput to create_job kwargs
            name = body.get("name", "")
            schedule = body.get("schedule", {})
            cron_expr = schedule.get("cron", "") if isinstance(schedule, dict) else ""
            timezone = schedule.get("timezone", "Asia/Shanghai") if isinstance(schedule, dict) else "Asia/Shanghai"
            dispatch = body.get("dispatch", {})
            request_body = dispatch.get("target", {})
            session_id = request_body.get("session_id")
            user_id = request_body.get("user_id", "default")
            task_type = body.get("task_type", "agent")
            text = body.get("text", "")
            runtime = body.get("runtime", {})
            timeout = runtime.get("timeout_seconds", 120)

            job = self._cron_create(
                prompt=text,
                schedule=cron_expr,
                name=name,
            )
            return web.json_response(job, status=201)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_cronjob(self, request: "web.Request") -> "web.Response":
        """PATCH /cronjobs/{job_id}"""
        if not self._CRON_AVAILABLE:
            return web.json_response({"error": "Cron not available"}, status=501)
        job_id = request.match_info.get("job_id", "")
        try:
            body = await request.json()
            job = self._cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            # Partial update
            if "schedule" in body:
                job["schedule"] = body["schedule"]
            if "name" in body:
                job["name"] = body["name"]
            if "enabled" in body:
                job["enabled"] = body["enabled"]
            if "text" in body:
                job["text"] = body["text"]
            updated = self._cron_update(job_id, job)
            return web.json_response(updated)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_cronjob(self, request: "web.Request") -> "web.Response":
        """DELETE /cronjobs/{job_id}"""
        if not self._CRON_AVAILABLE:
            return web.json_response({"error": "Cron not available"}, status=501)
        job_id = request.match_info.get("job_id", "")
        try:
            success = self._cron_remove(job_id)
            return web.json_response({"success": bool(success)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_cronjob(self, request: "web.Request") -> "web.Response":
        """POST /cronjobs/{id}/pause"""
        if not self._CRON_AVAILABLE:
            return web.json_response({"error": "Cron not available"}, status=501)
        job_id = request.match_info.get("job_id", "")
        try:
            job = self._cron_pause(job_id)
            if job:
                return web.json_response(job)
            return web.json_response({"error": "Job not found"}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_cronjob(self, request: "web.Request") -> "web.Response":
        """POST /cronjobs/{id}/resume"""
        if not self._CRON_AVAILABLE:
            return web.json_response({"error": "Cron not available"}, status=501)
        job_id = request.match_info.get("job_id", "")
        try:
            job = self._cron_resume(job_id)
            if job:
                return web.json_response(job)
            return web.json_response({"error": "Job not found"}, status=404)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # Custom providers (for Settings → Models page) ----------------------------
    def _custom_providers_file(self) -> Path:
        return Path.home() / ".sinoclaw" / "custom_providers.json"

    def _load_custom_providers(self) -> list:
        f = self._custom_providers_file()
        if f.exists():
            import json as _json
            return _json.loads(f.read_text(encoding="utf-8"))
        return []

    def _save_custom_providers(self, providers: list) -> None:
        import json as _json
        f = self._custom_providers_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_json.dumps(providers, indent=2), encoding="utf-8")

    async def _handle_list_custom_providers(self, request: "web.Request") -> "web.Response":
        """GET /models/custom-providers"""
        return web.json_response(self._load_custom_providers())

    async def _handle_create_custom_provider(self, request: "web.Request") -> "web.Response":
        """POST /models/custom-providers"""
        try:
            body = await request.json()
            providers = self._load_custom_providers()
            new_provider = {
                "id": body.get("id", ""),
                "name": body.get("name", ""),
                "api_key": body.get("api_key", ""),
                "base_url": body.get("base_url", ""),
                "models": body.get("models", []),
            }
            providers = [p for p in providers if p.get("id") != new_provider["id"]]
            providers.append(new_provider)
            self._save_custom_providers(providers)
            return web.json_response(new_provider, status=201)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_custom_provider(self, request: "web.Request") -> "web.Response":
        """DELETE /models/custom-providers/{id}"""
        provider_id = request.match_info.get("provider_id", "")
        try:
            providers = self._load_custom_providers()
            providers = [p for p in providers if p.get("id") != provider_id]
            self._save_custom_providers(providers)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_list_providers(self, request: "web.Request") -> "web.Response":
        """GET /models — List model providers."""
        import os as _os
        api_key = _os.getenv("MINIMAX_API_KEY", "")
        prefix = api_key[:8] + "..." if api_key else ""
        return web.json_response([{
            "id": "minimax-cn",
            "name": "MiniMax",
            "api_key_prefix": "sk-cp-",
            "chat_model": "MiniMax-M2.7-High-Speed",
            "models": [
                {"id": "MiniMax-M2.7-High-Speed", "name": "MiniMax M2.7 High Speed",
                 "supports_multimodal": False, "supports_image": False, "supports_video": False,
                 "is_free": False, "generate_kwargs": {}},
                {"id": "MiniMax-M2.7-Standard", "name": "MiniMax M2.7 Standard",
                 "supports_multimodal": False, "supports_image": False, "supports_video": False,
                 "is_free": False, "generate_kwargs": {}},
            ],
            "extra_models": [],
            "is_custom": False,
            "is_local": False,
            "support_model_discovery": False,
            "support_connection_check": True,
            "freeze_url": True,
            "require_api_key": True,
            "api_key": prefix,
            "base_url": "https://api.minimaxi.com",
            "generate_kwargs": {}
        }])

    async def _handle_get_active_models(self, request: "web.Request") -> "web.Response":
        """GET /models/active — Get active model config."""
        return web.json_response({
            "active_llm": {"provider_id": "minimax-cn", "model": "MiniMax-M2.7-High-Speed"},
            "active_vision": None,
            "active_audio": None,
        })

    def _update_env_var(self, key: str, value: str) -> None:
        """Update or add an env var in ~/.sinoclaw/.env."""
        env_file = Path.home() / ".sinoclaw" / ".env"
        lines = []
        found = False
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}")
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append(f"{key}={value}")
        env_file.write_text("\n".join(lines), encoding="utf-8")

    async def _handle_set_active_llm(self, request: "web.Request") -> "web.Response":
        """PUT /models/active"""
        try:
            body = await request.json()
            active_llm = body.get("active_llm", {})
            model = active_llm.get("model", "")
            if model:
                self._update_env_var("SINOCLAW_MODEL", model)
            return web.json_response({
                "active_llm": active_llm,
                "active_vision": None,
                "active_audio": None,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_configure_provider(self, request: "web.Request") -> "web.Response":
        """PUT /models/{id}/config"""
        provider_id = request.match_info.get("provider_id", "minimax-cn")
        try:
            body = await request.json()
            api_key = body.get("api_key", "")
            base_url = body.get("base_url", "")
            if api_key and provider_id == "minimax-cn":
                self._update_env_var("MINIMAX_API_KEY", api_key)
            if base_url:
                self._update_env_var("MINIMAX_BASE_URL", base_url)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_test_provider(self, request: "web.Request") -> "web.Response":
        """POST /models/{id}/test"""
        provider_id = request.match_info.get("provider_id", "minimax-cn")
        try:
            import os as _os
            api_key = _os.getenv("MINIMAX_API_KEY", "")
            if not api_key:
                # Try to read from request body
                try:
                    body = await request.json()
                    api_key = body.get("api_key", "")
                except Exception:
                    pass
            if api_key and api_key not in ("", "test"):
                # Simple connectivity check
                return web.json_response({"success": True, "message": "connection ok"})
            return web.json_response({"success": False, "message": "no api key"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_discover_models(self, request: "web.Request") -> "web.Response":
        """POST /models/{id}/discover"""
        # Return the known MiniMax models as "discovered"
        return web.json_response([
            {"id": "MiniMax-M2.7-High-Speed", "name": "MiniMax M2.7 High Speed", "supports_multimodal": False},
            {"id": "MiniMax-M2.7-Standard", "name": "MiniMax M2.7 Standard", "supports_multimodal": False},
        ])

    async def _handle_local_models_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT /local-models/config"""
        if request.method == "PUT":
            return web.json_response({"success": True})
        return web.json_response({"enabled": False, "server_url": ""})

    async def _handle_openrouter_series(self, request: "web.Request") -> "web.Response":
        """GET /models/openrouter/series"""
        return web.json_response([])

    async def _handle_settings_language(self, request: "web.Request") -> "web.Response":
        """GET/PUT /settings/language"""
        return web.json_response({"language": "zh-CN"})

    # Environment variables (for Settings → Environment Variables page) ---------
    def _read_env_file(self) -> dict:
        """Read ~/.sinoclaw/.env and return as dict."""
        env_file = Path.home() / ".sinoclaw" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        if not env_file.exists():
            return {}
        result = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                result[k.strip()] = v.strip()
        return result

    def _write_env_file(self, vars: dict) -> None:
        """Write dict to ~/.sinoclaw/.env."""
        env_file = Path.home() / ".sinoclaw" / ".env"
        lines = []
        for k, v in vars.items():
            lines.append(f"{k}={v}")
        env_file.write_text("\n".join(lines), encoding="utf-8")

    async def _handle_list_envs(self, request: "web.Request") -> "web.Response":
        """GET /envs"""
        try:
            envs = self._read_env_file()
            result = [{"key": k, "value": v} for k, v in envs.items()]
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_save_envs(self, request: "web.Request") -> "web.Response":
        """PUT /envs — full replacement of all env vars."""
        try:
            body = await request.json()
            if isinstance(body, dict):
                env_vars = body
            else:
                env_vars = {item["key"]: item["value"] for item in body if item.get("key")}
            self._write_env_file(env_vars)
            result = [{"key": k, "value": v} for k, v in env_vars.items()]
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_env(self, request: "web.Request") -> "web.Response":
        """DELETE /envs/{key}"""
        key = request.match_info.get("key", "")
        if not key:
            return web.json_response({"error": "key required"}, status=400)
        try:
            envs = self._read_env_file()
            if key in envs:
                del envs[key]
                self._write_env_file(envs)
            result = [{"key": k, "value": v} for k, v in envs.items()]
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_console_push_messages(self, request: "web.Request") -> "web.Response":
        """GET /console/push-messages"""
        return web.json_response({"messages": []})


    async def _handle_refresh_skills(self, request: "web.Request") -> "web.Response":
        """POST /skills/refresh"""
        return web.json_response([])

    async def _handle_batch_enable_skills(self, request: "web.Request") -> "web.Response":
        """POST /skills/batch-enable"""
        return web.json_response({})

    async def _handle_batch_delete_skills(self, request: "web.Request") -> "web.Response":
        """POST /skills/batch-delete"""
        try:
            body = await request.json()
            results = {}
            for name in body:
                results[name] = {"success": True}
            return web.json_response({"results": results})
        except Exception:
            return web.json_response({"results": {}})

    async def _handle_upload_skill(self, request: "web.Request") -> "web.Response":
        return web.json_response({"imported": [], "count": 0, "enabled": False})

    async def _handle_save_pool_skill(self, request: "web.Request") -> "web.Response":
        """PUT /skills/pool/save"""
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            content = body.get("content", "")
            desc = body.get("description", "")
            if not name:
                return web.json_response({"error": "name required"}, status=400)
            skills_dir = Path.home() / ".sinoclaw" / "skills" / name
            skills_dir.mkdir(parents=True, exist_ok=True)
            if desc is not None:
                (skills_dir / "DESCRIPTION.md").write_text("---\ndescription: " + (desc or "") + "\n---\n", encoding="utf-8")
            if content is not None:
                (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
            return web.json_response({"success": True, "mode": "edit", "name": name})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_pool_skill(self, request: "web.Request") -> "web.Response":
        """POST /skills/pool/create"""
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            content = body.get("content", "# " + name + "\n")
            desc = body.get("description", "")
            if not name:
                return web.json_response({"error": "name required"}, status=400)
            skills_dir = Path.home() / ".sinoclaw" / "skills" / name
            if skills_dir.exists():
                return web.json_response({"error": "Skill already exists"}, status=409)
            skills_dir.mkdir(parents=True, exist_ok=True)
            (skills_dir / "DESCRIPTION.md").write_text("---\ndescription: " + (desc or "") + "\n---\n", encoding="utf-8")
            (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
            return web.json_response({"created": True, "name": name}, status=201)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_pool_skill(self, request: "web.Request") -> "web.Response":
        """DELETE /skills/pool/{skill_name}"""
        skill_name = request.match_info.get("skill_name", "")
        if not skill_name:
            return web.json_response({"error": "name required"}, status=400)
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
            # Don't delete protected skills
            if skills_dir.as_posix().startswith("/data/sinoclaw/skills"):
                return web.json_response({"error": "Cannot delete protected skill"}, status=403)
            if skills_dir.exists():
                import shutil as _shutil
                _shutil.rmtree(skills_dir)
            return web.json_response({"deleted": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pool_skill_tags(self, request: "web.Request") -> "web.Response":
        """GET/PUT /skills/pool/{skill_name}/tags"""
        skill_name = request.match_info.get("skill_name", "")
        skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
        tags_file = skills_dir / "tags.json"
        if request.method == "GET":
            if tags_file.exists():
                import json as _json
                return web.json_response({"tags": _json.loads(tags_file.read_text(encoding="utf-8"))})
            return web.json_response({"tags": []})
        elif request.method == "PUT":
            try:
                body = await request.json()
                tags = body if isinstance(body, list) else body.get("tags", [])
                import json as _json
                tags_file.write_text(_json.dumps(tags, indent=2), encoding="utf-8")
                return web.json_response({"updated": True, "tags": tags})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"error": "method not allowed"}, status=405)

    async def _handle_pool_skill_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT/DELETE /skills/pool/{skill_name}/config"""
        skill_name = request.match_info.get("skill_name", "")
        skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
        config_file = skills_dir / "config.json"
        if request.method == "GET":
            if config_file.exists():
                import json as _json
                cfg = _json.loads(config_file.read_text(encoding="utf-8"))
                return web.json_response({"config": cfg})
            return web.json_response({"config": {}})
        elif request.method == "PUT":
            try:
                body = await request.json()
                cfg = body.get("config", {})
                import json as _json
                skills_dir.mkdir(parents=True, exist_ok=True)
                config_file.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")
                return web.json_response({"updated": True})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        elif request.method == "DELETE":
            if config_file.exists():
                config_file.unlink()
            return web.json_response({"cleared": True})
        return web.json_response({"error": "method not allowed"}, status=405)

    async def _handle_pool_builtin_notice(self, request: "web.Request") -> "web.Response":
        """GET /skills/pool/builtin-notice"""
        return web.json_response({
            "fingerprint": "",
            "has_updates": False,
            "total_changes": 0,
            "actionable_skill_names": [],
            "added": [],
            "missing": [],
            "updated": [],
            "removed": [],
        })

    async def _handle_pool_builtin_sources(self, request: "web.Request") -> "web.Response":
        """GET /skills/pool/builtin-sources"""
        return web.json_response([])

    async def _handle_pool_import_builtin(self, request: "web.Request") -> "web.Response":
        """POST /skills/pool/import-builtin"""
        return web.json_response({"imported": [], "updated": [], "unchanged": [], "conflicts": []})

    async def _handle_pool_update_builtin(self, request: "web.Request") -> "web.Response":
        """POST /skills/pool/{skill_name}/update-builtin"""
        skill_name = request.match_info.get("skill_name", "")
        return web.json_response({"updated": True, "name": skill_name})

    async def _handle_pool_download(self, request: "web.Request") -> "web.Response":
        """POST /skills/pool/download"""
        return web.json_response({"downloaded": [], "conflicts": []})

    async def _handle_pool_upload_zip(self, request: "web.Request") -> "web.Response":
        """POST /skills/pool/upload-zip"""
        return web.json_response({"imported": [], "count": 0, "conflicts": []})

    async def _handle_skill_pool_refresh(self, request: "web.Request") -> "web.Response":
        """POST /skills/pool/refresh"""
        return web.json_response([])

    # Skills pool and workspaces (stub for now) ---------------------------------
    async def _handle_list_skill_pool(self, request: "web.Request") -> "web.Response":
        """GET /skills/pool — List all available pool skills."""
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills"
            if not skills_dir.exists():
                return web.json_response([])
            pool = []
            for skill_path in sorted(skills_dir.iterdir()):
                if not skill_path.is_dir():
                    continue
                desc_file = skill_path / "DESCRIPTION.md"
                desc = ""
                if desc_file.exists():
                    desc = desc_file.read_text(encoding="utf-8").strip()
                # Check if enabled (has SKILL.md)
                skill_md = skill_path / "SKILL.md"
                # Get first sub-skill's content if no root SKILL.md
                content = ""
                if skill_md.exists():
                    content = skill_md.read_text(encoding="utf-8").strip()
                else:
                    sub_skills = [d for d in skill_path.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
                    if sub_skills:
                        content = (sub_skills[0] / "SKILL.md").read_text(encoding="utf-8").strip()
                # Check config
                config_file = skill_path / "config.json"
                config = {}
                if config_file.exists():
                    import json as _json
                    config = _json.loads(config_file.read_text(encoding="utf-8"))
                # Check tags
                tags_file = skill_path / "tags.json"
                tags = []
                if tags_file.exists():
                    import json as _json
                    tags = _json.loads(tags_file.read_text(encoding="utf-8"))
                # Protected = bundled with sinoclaw (in /data/sinoclaw/skills)
                protected = skill_path.as_posix().startswith("/data/sinoclaw/skills")
                pool.append({
                    "name": skill_path.name,
                    "description": desc,
                    "content": content,
                    "source": "local",
                    "protected": protected,
                    "enabled": skill_md.exists(),
                    "tags": tags,
                    "config": config,
                    "sync_status": "synced" if not protected else "-",
                })
            return web.json_response(pool)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_list_skill_workspaces(self, request: "web.Request") -> "web.Response":
        """GET /skills/workspaces"""
        # Return the single local workspace
        return web.json_response([{
            "agent_id": "default",
            "workspace_dir": "/root/.sinoclaw",
            "skills": [],
        }])

    # Skills management -------------------------------------------------------
    async def _handle_list_skills(self, request: "web.Request") -> "web.Response":
        """GET /skills — List all installed skills."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills"
            if not skills_dir.exists():
                return web.json_response([])
            skills = []
            for skill_path in sorted(skills_dir.iterdir()):
                if not skill_path.is_dir():
                    continue
                desc_file = skill_path / "DESCRIPTION.md"
                desc = ""
                if desc_file.exists():
                    try:
                        desc = desc_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
                # Check if skill has a SKILL.md (enabled indicator)
                skill_md = skill_path / "SKILL.md"
                enabled = skill_md.exists()
                skills.append({
                    "name": skill_path.name,
                    "description": desc,
                    "enabled": enabled,
                    "source": "local",
                    "channels": [],
                    "tags": [],
                })
            return web.json_response(skills)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_skill(self, request: "web.Request") -> "web.Response":
        """GET /skills/{skill_name}"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        skill_name = request.match_info.get("skill_name", "")
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
            if not skills_dir.exists():
                return web.json_response({"error": "Skill not found"}, status=404)
            desc_file = skills_dir / "DESCRIPTION.md"
            desc = ""
            if desc_file.exists():
                desc = desc_file.read_text(encoding="utf-8").strip()
            skill_md = skills_dir / "SKILL.md"
            content = ""
            if skill_md.exists():
                content = skill_md.read_text(encoding="utf-8").strip()
            return web.json_response({
                "name": skill_name,
                "description": desc,
                "content": content,
                "enabled": skill_md.exists(),
                "source": "local",
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_skill(self, request: "web.Request") -> "web.Response":
        """POST /skills"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            content = body.get("content", "")
            config = body.get("config")
            enable = body.get("enable", True)
            if not name:
                return web.json_response({"error": "Skill name required"}, status=400)
            skills_dir = Path.home() / ".sinoclaw" / "skills" / name
            skills_dir.mkdir(parents=True, exist_ok=True)
            _desc = body.get("description", "")
            desc_content = "---\ndescription: " + _desc + "\n---"
            (skills_dir / "DESCRIPTION.md").write_text(desc_content, encoding="utf-8")
            if content and enable:
                (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
            return web.json_response({"created": True, "name": name}, status=201)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_save_skill(self, request: "web.Request") -> "web.Response":
        """PUT /skills/save"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            content = body.get("content", "")
            desc = body.get("description", "")
            if not name:
                return web.json_response({"error": "Skill name required"}, status=400)
            skills_dir = Path.home() / ".sinoclaw" / "skills" / name
            skills_dir.mkdir(parents=True, exist_ok=True)
            if desc is not None:
                desc_content = "---\ndescription: " + (desc or "") + "\n---"
                (skills_dir / "DESCRIPTION.md").write_text(desc_content, encoding="utf-8")
            if content is not None:
                (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
            return web.json_response({"success": True, "mode": "edit", "name": name})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_skill(self, request: "web.Request") -> "web.Response":
        """DELETE /skills/{skill_name}"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        skill_name = request.match_info.get("skill_name", "")
        if not skill_name:
            return web.json_response({"error": "Skill name required"}, status=400)
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
            if skills_dir.exists():
                import shutil as _shutil
                _shutil.rmtree(skills_dir)
            return web.json_response({"deleted": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_enable_skill(self, request: "web.Request") -> "web.Response":
        """POST /skills/{skill_name}/enable"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        skill_name = request.match_info.get("skill_name", "")
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
            if not skills_dir.exists():
                return web.json_response({"error": "Skill not found"}, status=404)
            skill_md = skills_dir / "SKILL.md"
            if not skill_md.exists():
                # Try to find content from sub-skills
                sub_dirs = [d for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
                if sub_dirs:
                    first_skill = sub_dirs[0] / "SKILL.md"
                    content = first_skill.read_text(encoding="utf-8")
                    skill_md.write_text(content, encoding="utf-8")
                else:
                    skill_md.write_text("# " + skill_name + "\n", encoding="utf-8")
            return web.json_response({"enabled": True, "name": skill_name})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_disable_skill(self, request: "web.Request") -> "web.Response":
        """POST /skills/{skill_name}/disable"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        skill_name = request.match_info.get("skill_name", "")
        try:
            skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
            if not skills_dir.exists():
                return web.json_response({"error": "Skill not found"}, status=404)
            skill_md = skills_dir / "SKILL.md"
            if skill_md.exists():
                skill_md.unlink()
            return web.json_response({"disabled": True, "name": skill_name})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_skill_config(self, request: "web.Request") -> "web.Response":
        """GET/PUT/DELETE /skills/{skill_name}/config"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        skill_name = request.match_info.get("skill_name", "")
        skills_dir = Path.home() / ".sinoclaw" / "skills" / skill_name
        config_file = skills_dir / "config.json"
        if request.method == "GET":
            if not skills_dir.exists():
                return web.json_response({"error": "Skill not found"}, status=404)
            if config_file.exists():
                import json as _json
                cfg = _json.loads(config_file.read_text(encoding="utf-8"))
                return web.json_response({"config": cfg})
            return web.json_response({"config": {}})
        elif request.method == "PUT":
            try:
                body = await request.json()
                cfg = body.get("config", {})
                skills_dir.mkdir(parents=True, exist_ok=True)
                import json as _json
                config_file.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")
                return web.json_response({"updated": True})
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)
        elif request.method == "DELETE":
            if config_file.exists():
                config_file.unlink()
            return web.json_response({"cleared": True})
        return web.json_response({"error": "Method not allowed"}, status=405)

    def _load_mcp_clients(self) -> dict:
        """Load MCP clients from JSON file."""
        try:
            f = open(str(Path.home() / ".sinoclaw" / "mcp_clients.json"), "r")
            import json as _json
            return _json.load(f)
        except Exception:
            return {}

    def _save_mcp_clients(self, clients: dict) -> None:
        """Save MCP clients to JSON file."""
        try:
            import json as _json
            path = Path.home() / ".sinoclaw" / "mcp_clients.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(str(path), "w") as f:
                _json.dump(clients, f, indent=2)
        except Exception:
            pass

    async def _handle_mcp_list(self, request: "web.Request") -> "web.Response":
        """GET /mcp — List all MCP clients."""
        clients = self._load_mcp_clients()
        result = []
        for key, cfg in clients.items():
            result.append({
                "key": key,
                "name": cfg.get("name", key),
                "description": cfg.get("description", ""),
                "enabled": cfg.get("enabled", True),
                "transport": cfg.get("transport", "stdio"),
                "url": cfg.get("url", ""),
                "headers": cfg.get("headers", {}),
                "command": cfg.get("command", ""),
                "args": cfg.get("args", []),
                "env": cfg.get("env", {}),
                "cwd": cfg.get("cwd", ""),
            })
        return web.json_response(result)

    async def _handle_mcp_create(self, request: "web.Request") -> "web.Response":
        """POST /mcp — Create MCP client."""
        try:
            body = await request.json()
            client_key = body.get("client_key", "")
            client_cfg = body.get("client", {})
            if not client_key:
                return web.json_response({"error": "client_key required"}, status=400)
            clients = self._load_mcp_clients()
            if client_key in clients:
                return web.json_response({"error": "client already exists"}, status=409)
            clients[client_key] = {
                "name": client_cfg.get("name", client_key),
                "description": client_cfg.get("description", ""),
                "enabled": client_cfg.get("enabled", True),
                "transport": client_cfg.get("transport", "stdio"),
                "url": client_cfg.get("url", ""),
                "headers": client_cfg.get("headers", {}),
                "command": client_cfg.get("command", ""),
                "args": client_cfg.get("args", []),
                "env": client_cfg.get("env", {}),
                "cwd": client_cfg.get("cwd", ""),
            }
            self._save_mcp_clients(clients)
            result = clients[client_key].copy()
            result["key"] = client_key
            return web.json_response(result, status=201)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_mcp_get(self, request: "web.Request") -> "web.Response":
        """GET /mcp/{clientKey}"""
        client_key = request.match_info.get("clientKey", "")
        clients = self._load_mcp_clients()
        if client_key not in clients:
            return web.json_response({"error": "not found"}, status=404)
        result = clients[client_key].copy()
        result["key"] = client_key
        return web.json_response(result)

    async def _handle_mcp_update(self, request: "web.Request") -> "web.Response":
        """PUT /mcp/{clientKey}"""
        client_key = request.match_info.get("clientKey", "")
        clients = self._load_mcp_clients()
        if client_key not in clients:
            return web.json_response({"error": "not found"}, status=404)
        try:
            body = await request.json()
            cfg = clients[client_key]
            for field in ("name", "description", "enabled", "transport", "url", "headers", "command", "args", "env", "cwd"):
                if field in body:
                    cfg[field] = body[field]
            self._save_mcp_clients(clients)
            result = cfg.copy()
            result["key"] = client_key
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_mcp_delete(self, request: "web.Request") -> "web.Response":
        """DELETE /mcp/{clientKey}"""
        client_key = request.match_info.get("clientKey", "")
        clients = self._load_mcp_clients()
        if client_key in clients:
            del clients[client_key]
            self._save_mcp_clients(clients)
        return web.json_response({"message": "deleted"})

    async def _handle_mcp_toggle(self, request: "web.Request") -> "web.Response":
        """PATCH /mcp/{clientKey}/toggle"""
        client_key = request.match_info.get("clientKey", "")
        clients = self._load_mcp_clients()
        if client_key not in clients:
            return web.json_response({"error": "not found"}, status=404)
        clients[client_key]["enabled"] = not clients[client_key].get("enabled", True)
        self._save_mcp_clients(clients)
        result = clients[client_key].copy()
        result["key"] = client_key
        return web.json_response(result)

    async def _handle_mcp_tools(self, request: "web.Request") -> "web.Response":
        """GET /mcp/{clientKey}/tools — List tools from MCP server."""
        # Return empty list — actual MCP tool discovery requires server connection
        return web.json_response([])

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed"""
        return web.json_response({"status": "ok", "platform": "sinoclaw-agent"})



    async def _handle_console_chat(self, request: "web.Request") -> "web.Response":
        """POST /console/chat — Streaming chat endpoint used by Console UI."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        input_messages = body.get("input", [])
        session_id = body.get("session_id", "")
        user_id = body.get("user_id", "default")
        channel = body.get("channel", "console")
        stream = body.get("stream", True)

        # Convert input messages to conversation format
        conversation_messages = []
        system_prompt = None
        for msg in input_messages:
            role = msg.get("role", "user")
            content = ""
            if isinstance(msg.get("content"), str):
                content = msg.get("content", "")
            elif isinstance(msg.get("content"), list):
                # Handle multimodal content blocks
                parts = []
                for block in msg.get("content", []):
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif block.get("type") == "image_url":
                            parts.append("[image]")
                    elif isinstance(block, str):
                        parts.append(block)
                content = "\n".join(parts)
            if role == "system":
                system_prompt = content
            elif role in ("user", "assistant"):
                conversation_messages.append({"role": role, "content": content})

        # Get last user message
        user_message = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not user_message:
            return web.json_response({"error": "No user message found"}, status=400)

        # Generate session_id if not provided
        if not session_id:
            import time as _time, uuid as _uuid
            session_id = f"{_time.strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:8]}"
        else:
            # Load existing session history from DB
            db = self._ensure_session_db()
            if db is not None:
                try:
                    existing = db.get_messages_as_conversation(session_id)
                    if existing:
                        history = existing
                except Exception:
                    pass

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        created = int(time.time())
        model_name = self._model_name

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                if event_type != "tool.started":
                    return
                if name.startswith("_"):
                    return
                try:
                    from agent.display import get_tool_emoji
                    emoji = get_tool_emoji(name)
                except Exception:
                    emoji = "🔧"
                label = preview or name
                _stream_q.put(("__tool_progress__", {
                    "tool": name,
                    "emoji": emoji,
                    "label": label,
                }))

            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                agent_ref=agent_ref,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
            )

        # Non-streaming
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
        )
        # Unwrap final_response from the agent result dict
        if isinstance(result, dict):
            final_text = result.get("final_response", str(result))
        else:
            final_text = str(result) if result else ""
        return web.json_response({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": final_text},
                "finish_reason": "stop",
            }],
            "usage": usage,
        })


    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            mws = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws)
            self._app["api_server_adapter"] = self
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/health/detailed", self._handle_health_detailed)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            # Cron jobs management API
            self._app.router.add_get("/api/jobs", self._handle_list_jobs)
            self._app.router.add_post("/api/jobs", self._handle_create_job)
            self._app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
            self._app.router.add_patch("/api/jobs/{job_id}", self._handle_update_job)
            self._app.router.add_delete("/api/jobs/{job_id}", self._handle_delete_job)
            self._app.router.add_post("/api/jobs/{job_id}/pause", self._handle_pause_job)
            self._app.router.add_post("/api/jobs/{job_id}/resume", self._handle_resume_job)
            self._app.router.add_post("/api/jobs/{job_id}/run", self._handle_run_job)
            # Structured event streaming
            self._app.router.add_post("/v1/runs", self._handle_runs)
            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
            # Chat session management API (Console UI)
            # Agent files / memory
            self._app.router.add_get("/agent/files", self._handle_list_agent_files)
            self._app.router.add_get("/agents/{agent_id}/files", self._handle_list_agent_files)
            self._app.router.add_get("/agents/{agent_id}/files/{filename}", self._handle_read_agent_file)
            self._app.router.add_put("/agents/{agent_id}/files/{filename}", self._handle_write_agent_file)
            self._app.router.add_get("/agents/{agent_id}/memory", self._handle_list_agent_memory)
            self._app.router.add_get("/agent/memory/{date}.md", self._handle_read_agent_memory)
            self._app.router.add_put("/agent/memory/{date}.md", self._handle_write_agent_memory)
            self._app.router.add_get("/agents/{agent_id}/memory/{date}.md", self._handle_read_agent_memory)
            self._app.router.add_put("/agents/{agent_id}/memory/{date}.md", self._handle_write_agent_memory)
            self._app.router.add_get("/agent/files/{filename}", self._handle_read_agent_file)
            self._app.router.add_put("/agent/files/{filename}", self._handle_write_agent_file)
            self._app.router.add_get("/agent/memory", self._handle_list_agent_memory)
            self._app.router.add_get("/agent/system-prompt-files", self._handle_get_system_prompt_files)
            self._app.router.add_put("/agent/system-prompt-files", self._handle_set_system_prompt_files)
            self._app.router.add_get("/workspace/download", self._handle_workspace_download)
            self._app.router.add_post("/workspace/upload", self._handle_workspace_upload)
            # Tools
            self._app.router.add_get("/tools", self._handle_list_tools)
            self._app.router.add_patch("/tools/{tool_name}/toggle", self._handle_toggle_tool)
            self._app.router.add_put("/tools/{tool_name}/async-execution", self._handle_tool_async_execution)
            # Token usage
            self._app.router.add_get("/token-usage", self._handle_token_usage)
            # Security config
            # Security: tool guard
            self._app.router.add_get("/config/security/tool-guard", self._handle_tool_guard_config)
            self._app.router.add_put("/config/security/tool-guard", self._handle_tool_guard_config)
            self._app.router.add_get("/config/security/tool-guard/builtin-rules", self._handle_tool_guard_builtin_rules)
            # Security: file guard
            self._app.router.add_get("/config/security/file-guard", self._handle_file_guard_config)
            self._app.router.add_put("/config/security/file-guard", self._handle_file_guard_config)
            # Security: skill scanner
            self._app.router.add_get("/config/security/skill-scanner", self._handle_skill_scanner_config)
            self._app.router.add_put("/config/security/skill-scanner", self._handle_skill_scanner_config)
            self._app.router.add_get("/config/security/skill-scanner/blocked-history", self._handle_skill_scanner_blocked_history)
            self._app.router.add_delete("/config/security/skill-scanner/blocked-history", self._handle_skill_scanner_blocked_history)
            self._app.router.add_delete("/config/security/skill-scanner/blocked-history/{index}", self._handle_skill_scanner_blocked_history_delete)
            self._app.router.add_post("/config/security/skill-scanner/whitelist", self._handle_skill_scanner_whitelist_add)
            self._app.router.add_delete("/config/security/skill-scanner/whitelist/{skill_name}", self._handle_skill_scanner_whitelist_remove)
            # Security: combined (for Settings page overview)
            self._app.router.add_get("/config/security", self._handle_tool_guard_config)
            self._app.router.add_put("/config/security/tool-guard", self._handle_tool_guard_config)
            self._app.router.add_get("/config/security/file-guard", self._handle_file_guard_config)
            self._app.router.add_put("/config/security/file-guard", self._handle_file_guard_config)
            self._app.router.add_get("/config/security/skill-scanner", self._handle_skill_scanner_config)
            self._app.router.add_put("/config/security/skill-scanner", self._handle_skill_scanner_config)
            self._app.router.add_get("/config/channels", self._handle_config_channels)
            self._app.router.add_put("/config/channels", self._handle_config_channels)
            self._app.router.add_get("/config/channels/types", self._handle_config_channel_types)
            self._app.router.add_get("/config/channels/{channel}", self._handle_config_channel)
            self._app.router.add_put("/config/channels/{channel}", self._handle_config_channel)
            self._app.router.add_get("/config/channels/{channel}/qrcode", self._handle_config_channel_qrcode)
            self._app.router.add_get("/config/channels/{channel}/qrcode/status", self._handle_config_channel_qrcode_status)
            # Chats
            self._app.router.add_get("/chats", self._handle_list_chats)
            self._app.router.add_post("/chats", self._handle_create_chat)
            self._app.router.add_get("/chats/{chat_id}", self._handle_get_chat)
            self._app.router.add_put("/chats/{chat_id}", self._handle_update_chat)
            self._app.router.add_delete("/chats/{chat_id}", self._handle_delete_chat)
            self._app.router.add_post("/chats/batch-delete", self._handle_batch_delete_chats)
            # Console UI stub endpoints
            self._app.router.add_post("/console/chat", self._handle_console_chat)
            self._app.router.add_post("/console/chat/stop", self._handle_console_chat_stop)
            self._app.router.add_post("/console/upload", self._handle_console_upload)
            self._app.router.add_get("/files/preview/{name}", self._handle_files_preview)
            self._app.router.add_get("/agents", self._handle_list_agents)
            self._app.router.add_post("/agents", self._handle_create_agent)
            self._app.router.add_get("/agents/{agent_id}", self._handle_get_agent)
            self._app.router.add_put("/agents/{agent_id}", self._handle_update_agent)
            self._app.router.add_delete("/agents/{agent_id}", self._handle_delete_agent)
            self._app.router.add_put("/agents/order", self._handle_reorder_agents)
            self._app.router.add_patch("/agents/{agent_id}/toggle", self._handle_toggle_agent)
            self._app.router.add_get("/agents/{agent_id}/files", self._handle_agent_files)
            self._app.router.add_get("/agents/{agent_id}/memory", self._handle_agent_memory)
            self._app.router.add_get("/channels", self._handle_list_channels)
            self._app.router.add_get("/channels/{channel_id}", self._handle_get_channel)
            self._app.router.add_get("/cronjobs/{job_id}", self._handle_get_cronjob)
            self._app.router.add_get("/cronjobs", self._handle_list_cronjobs)
            self._app.router.add_post("/cronjobs", self._handle_create_cronjob)
            self._app.router.add_patch("/cronjobs/{job_id}", self._handle_update_cronjob)
            self._app.router.add_delete("/cronjobs/{job_id}", self._handle_delete_cronjob)
            self._app.router.add_post("/cronjobs/{job_id}/pause", self._handle_pause_cronjob)
            self._app.router.add_post("/cronjobs/{job_id}/resume", self._handle_resume_cronjob)
            self._app.router.add_get("/models/custom-providers", self._handle_list_custom_providers)
            self._app.router.add_post("/models/custom-providers", self._handle_create_custom_provider)
            self._app.router.add_delete("/models/custom-providers/{provider_id}", self._handle_delete_custom_provider)
            self._app.router.add_get("/models", self._handle_list_providers)
            self._app.router.add_get("/models/active", self._handle_get_active_models)
            self._app.router.add_put("/models/active", self._handle_set_active_llm)
            self._app.router.add_put("/models/{provider_id}/config", self._handle_configure_provider)
            self._app.router.add_post("/models/{provider_id}/test", self._handle_test_provider)
            self._app.router.add_post("/models/{provider_id}/discover", self._handle_discover_models)
            self._app.router.add_get("/local-models/config", self._handle_local_models_config)
            self._app.router.add_put("/local-models/config", self._handle_local_models_config)
            self._app.router.add_get("/models/openrouter/series", self._handle_openrouter_series)
            self._app.router.add_get("/settings/language", self._handle_settings_language)
            self._app.router.add_get("/envs", self._handle_list_envs)
            self._app.router.add_put("/envs", self._handle_save_envs)
            self._app.router.add_delete("/envs/{key}", self._handle_delete_env)
            self._app.router.add_put("/settings/language", self._handle_settings_language)
            self._app.router.add_get("/console/push-messages", self._handle_console_push_messages)
            # Skills routes
            self._app.router.add_post("/skills/refresh", self._handle_refresh_skills)
            self._app.router.add_post("/skills/batch-enable", self._handle_batch_enable_skills)
            self._app.router.add_post("/skills/batch-delete", self._handle_batch_delete_skills)
            self._app.router.add_post("/skills/upload", self._handle_upload_skill)
            self._app.router.add_get("/skills/pool", self._handle_list_skill_pool)
            self._app.router.add_post("/skills/pool/refresh", self._handle_list_skill_pool)
            self._app.router.add_post("/skills/pool/create", self._handle_create_pool_skill)
            self._app.router.add_put("/skills/pool/save", self._handle_save_pool_skill)
            self._app.router.add_delete("/skills/pool/{skill_name}", self._handle_delete_pool_skill)
            self._app.router.add_get("/skills/pool/{skill_name}/tags", self._handle_pool_skill_tags)
            self._app.router.add_put("/skills/pool/{skill_name}/tags", self._handle_pool_skill_tags)
            self._app.router.add_get("/skills/pool/{skill_name}/config", self._handle_pool_skill_config)
            self._app.router.add_put("/skills/pool/{skill_name}/config", self._handle_pool_skill_config)
            self._app.router.add_delete("/skills/pool/{skill_name}/config", self._handle_pool_skill_config)
            self._app.router.add_get("/skills/pool/builtin-notice", self._handle_pool_builtin_notice)
            self._app.router.add_post("/skills/pool/import-builtin", self._handle_pool_import_builtin)
            self._app.router.add_post("/skills/pool/{skill_name}/update-builtin", self._handle_pool_update_builtin)
            self._app.router.add_post("/skills/pool/download", self._handle_pool_download)
            self._app.router.add_post("/skills/pool/upload-zip", self._handle_pool_upload_zip)
            self._app.router.add_post("/skills/pool/refresh", self._handle_skill_pool_refresh)
            self._app.router.add_get("/skills/pool/builtin-sources", self._handle_pool_builtin_sources)
            self._app.router.add_get("/skills/workspaces", self._handle_list_skill_workspaces)
            self._app.router.add_get("/skills", self._handle_list_skills)
            self._app.router.add_post("/skills", self._handle_create_skill)
            self._app.router.add_put("/skills/save", self._handle_save_skill)
            self._app.router.add_get("/skills/{skill_name}", self._handle_get_skill)
            self._app.router.add_delete("/skills/{skill_name}", self._handle_delete_skill)
            self._app.router.add_post("/skills/{skill_name}/enable", self._handle_enable_skill)
            self._app.router.add_post("/skills/{skill_name}/disable", self._handle_disable_skill)
            self._app.router.add_get("/skills/{skill_name}/config", self._handle_skill_config)
            self._app.router.add_put("/skills/{skill_name}/config", self._handle_skill_config)
            self._app.router.add_delete("/skills/{skill_name}/config", self._handle_skill_config)
            self._app.router.add_get("/mcp", self._handle_mcp_list)
            self._app.router.add_post("/mcp", self._handle_mcp_create)
            self._app.router.add_get("/mcp/{clientKey}", self._handle_mcp_get)
            self._app.router.add_put("/mcp/{clientKey}", self._handle_mcp_update)
            self._app.router.add_delete("/mcp/{clientKey}", self._handle_mcp_delete)
            self._app.router.add_patch("/mcp/{clientKey}/toggle", self._handle_mcp_toggle)
            self._app.router.add_get("/mcp/{clientKey}/tools", self._handle_mcp_tools)
            # Start background sweep to clean up orphaned (unconsumed) run streams
            sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(sweep_task)
            except TypeError:
                pass
            if hasattr(sweep_task, "add_done_callback"):
                sweep_task.add_done_callback(self._background_tasks.discard)

            # Refuse to start network-accessible without authentication
            if is_network_accessible(self._host) and not self._api_key:
                logger.error(
                    "[%s] Refusing to start: binding to %s requires API_SERVER_KEY. "
                    "Set API_SERVER_KEY or use the default 127.0.0.1.",
                    self.name, self._host,
                )
                return False

            # Refuse to start network-accessible with a placeholder key.
            # Ported from openclaw/openclaw#64586.
            if is_network_accessible(self._host) and self._api_key:
                try:
                    from sinoclaw_cli.auth import has_usable_secret
                    if not has_usable_secret(self._api_key, min_length=8):
                        logger.error(
                            "[%s] Refusing to start: API_SERVER_KEY is set to a "
                            "placeholder value. Generate a real secret "
                            "(e.g. `openssl rand -hex 32`) and set API_SERVER_KEY "
                            "before exposing the API server on %s.",
                            self.name, self._host,
                        )
                        return False
                except ImportError:
                    pass

            # Port conflict detection — fail fast if port is already in use
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(1)
                    _s.connect(('127.0.0.1', self._port))
                logger.error('[%s] Port %d already in use. Set a different port in config.yaml: platforms.api_server.port', self.name, self._port)
                return False
            except (ConnectionRefusedError, OSError):
                pass  # port is free

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            if not self._api_key:
                logger.warning(
                    "[%s] ⚠️  No API key configured (API_SERVER_KEY / platforms.api_server.key). "
                    "All requests will be accepted without authentication. "
                    "Set an API key for production deployments to prevent "
                    "unauthorized access to sessions, responses, and cron jobs.",
                    self.name,
                )
            logger.info(
                "[%s] API server listening on http://%s:%d (model: %s)",
                self.name, self._host, self._port, self._model_name,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server."""
        self._mark_disconnected()
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
