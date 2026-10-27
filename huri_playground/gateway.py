"""Public demo gateway: visitors talk to HuRI, then answer a questionnaire.

The browser never reaches HuRI directly. HuRI builds whatever pipeline the
first message of a session describes and has no notion of a visitor, so this
gateway sits in between and owns everything a public page needs:

  * the pipeline: the browser ticks module names from what HuRI reports it
    runs (``GET /modules``); args stay server-side and the selection must form
    a pipeline the page can drive (see pipeline.py);
  * the cost: a cap on concurrent sessions and on session length;
  * abuse: an optional shared access code, a topic allow-list, per-topic rate
    and size limits;
  * the questionnaire: a session id handed out at ``ready`` is the one-time
    ticket to submit answers, so every response maps to a real conversation.

On the HuRI side the gateway is a client of the huri-core SDK
(``HuRIClient``): the SDK owns the websocket, the session handshake and the
framing. The gateway does not interpret the events it relays: each subscribed
topic is decoded into a pass-through event (the raw JSON object, or the raw
bytes) and framed again for the browser, unchanged.

Browser <-> gateway protocol (JSON text frames unless noted):

  -> {"type": "start", "modules": ["rag", "tts"], "options": {"qag": {...}},
      "access_code": "..."}
  <- {"type": "ready", "session_id", "max_seconds", "modules", "options",
      "inbound", "outbound"}
  <- {"type": "clock", "max_seconds"}   the visitor's first words: the
                                        session's time starts now
     | {"type": "error", "reason", "detail"?}   then close
  -> {"topic": "huri.question", "data": {"text": "..."}}   relayed to HuRI
  -> binary [u16 topic_len][topic][payload]                relayed to HuRI
  <- HuRI events, text or binary, relayed unchanged
  <- {"type": "rejected", "reason", "topic"}           frame dropped, keep going
  -> {"type": "finish"}
  <- {"type": "ended", "reason"}   then close
"""

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from huri_core.core.client import HuRIClient, HuRISessionError, decode, encode
from huri_core.core.events import BytesEvent, EventData
from starlette.websockets import WebSocketState

from .limits import RateLimiter, SessionSlots
from .pipeline import (
    RENDERED,
    Catalog,
    CatalogConfig,
    InvalidPipeline,
    Pipeline,
    load_catalog_config,
)
from .questionnaire import InvalidAnswers, Questionnaire, load_questionnaires
from .store import AlreadyAnswered, ResponseStore, SessionRecord, UnknownSession

logger = logging.getLogger("playground")

STATIC_DIR = Path(__file__).parent / "static"

START_TIMEOUT_S = 30.0
# Session creation instantiates every module of the pipeline on the HuRI side.
HURI_INIT_TIMEOUT_S = 60.0
# How long HuRI's module list is trusted: every page load asks for it.
CATALOG_TTL_S = 10.0
MAX_COMMENT_CHARS = 2000

QUESTION_TOPIC = "huri.question"


@dataclass
class RawJson(EventData[Mapping[str, Any]]):
    """A JSON event kept as the object it came as: the gateway relays it, it
    does not read it."""

    data: Mapping[str, Any]

    @classmethod
    def from_wire(cls, data: Mapping[str, Any]) -> "RawJson":
        return cls(data)

    def to_wire(self) -> Mapping[str, Any]:
        return self.data


@dataclass
class Settings:
    huri_url: str = "ws://localhost:8000/session"
    access_code: str = ""
    max_sessions: int = 2
    # The conversation's time counts from the visitor's first words (a typed
    # question, or anything HuRI sends back, which only answers what it heard);
    # a session nobody speaks in closes after idle_seconds.
    session_seconds: float = 300
    idle_seconds: float = 300
    max_frame_bytes: int = 64 * 1024
    data_dir: Path = Path("playground_data")
    questionnaires: List[str] = field(default_factory=lambda: ["godspeed", "rosas"])
    # Modules visitors may pick; None offers everything HuRI runs.
    modules: List[str] | None = None

    @property
    def huri_modules_url(self) -> str:
        """``ws://host:8000/session`` -> ``http://host:8000/modules``."""
        url = urlsplit(self.huri_url)
        scheme = {"ws": "http", "wss": "https"}.get(url.scheme, url.scheme)
        path = url.path.rsplit("/", 1)[0] + "/modules"
        return urlunsplit((scheme, url.netloc, path, "", ""))

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        default = cls()
        return cls(
            huri_url=env.get("HURI_URL", default.huri_url),
            access_code=env.get("PLAYGROUND_ACCESS_CODE", default.access_code),
            max_sessions=int(env.get("PLAYGROUND_MAX_SESSIONS", default.max_sessions)),
            session_seconds=float(
                env.get("PLAYGROUND_SESSION_SECONDS", default.session_seconds)
            ),
            idle_seconds=float(
                env.get("PLAYGROUND_IDLE_SECONDS", default.idle_seconds)
            ),
            data_dir=Path(env.get("PLAYGROUND_DATA_DIR", default.data_dir)),
            questionnaires=[
                q.strip()
                for q in env.get(
                    "PLAYGROUND_QUESTIONNAIRES", ",".join(default.questionnaires)
                ).split(",")
                if q.strip()
            ],
            modules=(
                _split(env["PLAYGROUND_MODULES"])
                if env.get("PLAYGROUND_MODULES")
                else None
            ),
        )


def _split(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


async def fetch_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


class Gateway:
    def __init__(
        self,
        settings: Settings,
        catalog_config: CatalogConfig,
        questionnaires: List[Questionnaire],
        store: ResponseStore,
        fetch: Callable[[str], Awaitable[Any]] = fetch_json,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.catalog_config = catalog_config
        self.questionnaires = questionnaires
        self.store = store
        self.slots = SessionSlots(settings.max_sessions)
        self.fetch = fetch
        self.clock = clock
        self._catalog: Tuple[float, Catalog] | None = None

    async def catalog(self) -> Catalog | None:
        """What visitors can pick, from the modules HuRI runs right now.

        Asked again after ``CATALOG_TTL_S``, so redeploying HuRI with other
        modules changes the page without restarting the playground. None when
        HuRI cannot be reached; a failure is not cached.
        """
        now = self.clock()
        if self._catalog is not None and now - self._catalog[0] < CATALOG_TTL_S:
            return self._catalog[1]
        try:
            reply = await self.fetch(self.settings.huri_modules_url)
            catalog = self.catalog_config.catalog(
                reply["details"], self.settings.modules
            )
        except Exception as e:  # noqa: BLE001 - down, old HuRI, bad reply
            logger.warning("playground: cannot list HuRI's modules: %r", e)
            return None
        self._catalog = (now, catalog)
        return catalog

    # --- HTTP -------------------------------------------------------------

    async def public_config(self) -> Dict[str, Any]:
        catalog = await self.catalog()
        return {
            "access_code_required": bool(self.settings.access_code),
            "max_seconds": self.settings.session_seconds,
            "huri_available": catalog is not None,
            **(
                catalog.public()
                if catalog is not None
                else {"modules": [], "inbound": [], "rendered": [], "selected": []}
            ),
            "questionnaires": [q.definition for q in self.questionnaires],
        }

    def submit(self, payload: Any) -> JSONResponse:
        if not isinstance(payload, Mapping):
            return _error(400, "body must be a JSON object")
        session_id = payload.get("session_id")
        answers = payload.get("answers")
        comment = payload.get("comment") or ""
        if not isinstance(session_id, str) or not isinstance(answers, Mapping):
            return _error(400, "session_id and answers are required")
        if not isinstance(comment, str) or len(comment) > MAX_COMMENT_CHARS:
            return _error(400, f"comment must be text under {MAX_COMMENT_CHARS}")

        expected = {q.id for q in self.questionnaires}
        if set(answers) != expected:
            return _error(400, f"answers must cover exactly {sorted(expected)}")
        try:
            clean = {q.id: q.validate(answers[q.id]) for q in self.questionnaires}
        except InvalidAnswers as e:
            return _error(400, str(e))
        scores = {q.id: q.score(clean[q.id]) for q in self.questionnaires if q.scores}

        try:
            self.store.submit(session_id, clean, comment.strip(), scores)
        except UnknownSession:
            return _error(404, "unknown or expired session")
        except AlreadyAnswered:
            return _error(409, "this session was already answered")
        return JSONResponse({"ok": True}, status_code=201)

    # --- WebSocket session ------------------------------------------------

    async def run_session(self, ws: WebSocket) -> None:
        await ws.accept()
        try:
            start = await asyncio.wait_for(ws.receive_json(), START_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - timeout, bad JSON or early disconnect
            await _close(ws)
            return

        if not isinstance(start, dict) or start.get("type") != "start":
            await _fail(ws, "expected_start")
            return
        if not self._access_ok(start.get("access_code")):
            await _fail(ws, "bad_access_code")
            return
        catalog = await self.catalog()
        if catalog is None:
            await _fail(ws, "huri_unavailable")
            return
        try:
            pipeline = catalog.build(start.get("modules"), start.get("options"))
        except InvalidPipeline as e:
            await _fail(ws, "invalid_pipeline", str(e))
            return
        if not self.slots.try_acquire():
            await _fail(ws, "busy")
            return

        try:
            await self._run_pipeline(ws, pipeline)
        finally:
            self.slots.release()

    def _access_ok(self, given: Any) -> bool:
        expected = self.settings.access_code
        if not expected:
            return True
        return isinstance(given, str) and hmac.compare_digest(
            given.encode(), expected.encode()
        )

    async def _run_pipeline(self, ws: WebSocket, pipeline: Pipeline) -> None:
        session_id = uuid.uuid4().hex
        user_id = f"playground-{session_id}"
        names = sorted(pipeline.modules)

        started = asyncio.Event()  # the visitor's first words
        huri = self._client(ws, pipeline, user_id, started)
        try:
            try:
                await asyncio.wait_for(huri.connect(), HURI_INIT_TIMEOUT_S)
            except HuRISessionError as e:
                logger.error(
                    "playground: HuRI rejected %s: %s (server runs %s)",
                    names,
                    e.error,
                    e.server_modules,
                )
                await _fail(ws, "huri_rejected")
                return
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                logger.warning("playground: HuRI unreachable: %r", e)
                await _fail(ws, "huri_unavailable")
                return

            record = self.store.open_session(session_id, names, pipeline.options)
            logger.info("playground: session %s started %s", session_id, names)
            await ws.send_json(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "max_seconds": self.settings.session_seconds,
                    "modules": names,
                    "options": pipeline.options,
                    "inbound": {
                        topic: {"max_chars": rule.max_chars}
                        for topic, rule in pipeline.inbound.items()
                    },
                    # What HuRI will stream: with huri.motion, the avatar gestures.
                    "outbound": pipeline.outbound,
                }
            )

            reason = await self._relay(ws, huri, pipeline, record, started)
            self.store.close_session(session_id, reason)
            logger.info("playground: session %s ended (%s)", session_id, reason)
        finally:
            # Shielded: the HuRI side is closed even when this handler is being
            # cancelled (the server shutting down), and that cancellation is not
            # swapped for one of the SDK's own tasks.
            await asyncio.shield(huri.close())

        if reason != "browser_left":
            await _send(ws, {"type": "ended", "reason": reason})
            await _close(ws)

    def _client(
        self, ws: WebSocket, pipeline: Pipeline, user_id: str, started: asyncio.Event
    ) -> HuRIClient:
        """The SDK client for this pipeline, every subscribed topic forwarded
        to the browser as it came, framed again by the SDK. HuRI only answers
        what it heard: its first event means the visitor started talking."""
        handshake = pipeline.handshake(user_id)
        client = HuRIClient(
            self.settings.huri_url,
            modules=handshake["modules"],
            sends=handshake["send"],
            user_id=user_id,
        )
        for topic in handshake["subscribe"]:
            client.on(topic, _event_class(topic), _forwarder(ws, topic, started))
        return client

    async def _relay(
        self,
        ws: WebSocket,
        huri: HuRIClient,
        pipeline: Pipeline,
        record: SessionRecord,
        started: asyncio.Event,
    ) -> str:
        """Pump frames both ways until one side stops or time runs out."""
        browser = asyncio.create_task(
            self._from_browser(ws, huri, pipeline, record, started)
        )
        server = asyncio.create_task(_until_huri_closes(huri))
        clock = asyncio.create_task(self._clock(ws, started))
        tasks = (browser, server, clock)

        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            # Reaped under a shield: awaiting a task one has just cancelled
            # while being cancelled oneself raises *its* CancelledError, which
            # the caller (the ASGI server) would not recognise as its own.
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))

        task = done.pop()
        if task.exception() is not None:
            logger.error("playground: relay failed", exc_info=task.exception())
            return "error"
        return str(task.result())

    async def _clock(self, ws: WebSocket, started: asyncio.Event) -> str:
        """The session's time, from the visitor's first words."""
        try:
            await asyncio.wait_for(started.wait(), self.settings.idle_seconds)
        except asyncio.TimeoutError:
            return "idle"
        await _send(ws, {"type": "clock", "max_seconds": self.settings.session_seconds})
        await asyncio.sleep(self.settings.session_seconds)
        return "time_limit"

    async def _from_browser(
        self,
        ws: WebSocket,
        huri: HuRIClient,
        pipeline: Pipeline,
        record: SessionRecord,
        started: asyncio.Event,
    ) -> str:
        limiters = {
            topic: RateLimiter(rule.max_per_minute)
            for topic, rule in pipeline.inbound.items()
        }

        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return "browser_left"

            if msg.get("bytes") is not None:
                frame: bytes = msg["bytes"]
                topic, payload = _binary_frame(frame)
                key = topic or ""
                rule = pipeline.inbound.get(key)
                if len(frame) > self.settings.max_frame_bytes:
                    await _reject(ws, "too_large", topic)
                elif rule is None:
                    await _reject(ws, "topic_not_allowed", topic)
                elif not rule.binary:
                    await _reject(ws, "bad_data", topic)
                elif not limiters[key].allow():
                    await _reject(ws, "rate_limited", topic)
                else:
                    await huri.send(key, BytesEvent(payload))
                continue

            try:
                event = json.loads(msg.get("text") or "")
            except ValueError:
                await _reject(ws, "bad_json", None)
                continue
            if not isinstance(event, dict):
                await _reject(ws, "bad_json", None)
                continue
            if event.get("type") == "finish":
                return "finished"

            topic = event.get("topic")
            data = event.get("data")
            key = topic if isinstance(topic, str) else ""
            rule = pipeline.inbound.get(key)
            if rule is None:
                await _reject(ws, "topic_not_allowed", topic)
            elif (
                rule.binary
                or not isinstance(data, dict)
                or not _text_ok(data, rule.max_chars)
            ):
                await _reject(ws, "bad_data", topic)
            elif not limiters[key].allow():
                await _reject(ws, "rate_limited", topic)
            else:
                await huri.send(key, RawJson(data))
                if key == QUESTION_TOPIC:
                    record.questions += 1
                    started.set()


def _event_class(topic: str) -> type:
    """How a subscribed topic is carried: bytes as they are, JSON as it is."""
    return BytesEvent if RENDERED.get(topic) == "binary" else RawJson


def _forwarder(
    ws: WebSocket, topic: str, started: asyncio.Event
) -> Callable[[Any], Awaitable[None]]:
    async def forward(event: EventData) -> None:
        started.set()
        frame = encode(topic, event)
        if isinstance(frame, bytes):
            await ws.send_bytes(frame)
        else:
            await ws.send_text(frame)

    return forward


async def _until_huri_closes(huri: HuRIClient) -> str:
    await huri.wait_closed()
    return "huri_closed"


def _binary_frame(frame: bytes) -> Tuple[str | None, bytes]:
    """(topic, payload) of a browser's binary frame; topic None when malformed."""
    if len(frame) < 2:
        return None, b""
    try:
        return decode(frame)
    except (UnicodeDecodeError, ValueError):
        return None, b""


def _text_ok(data: Mapping[str, Any], max_chars: int | None) -> bool:
    if max_chars is None:
        return True
    text = data.get("text")
    return isinstance(text, str) and 0 < len(text.strip()) and len(text) <= max_chars


async def _send(ws: WebSocket, message: Dict[str, Any]) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_json(message)
    except Exception:  # noqa: BLE001 - the browser may be gone already
        pass


async def _close(ws: WebSocket) -> None:
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass


async def _fail(ws: WebSocket, reason: str, detail: str | None = None) -> None:
    message = {"type": "error", "reason": reason}
    if detail:
        message["detail"] = detail
    await _send(ws, message)
    await _close(ws)


async def _reject(ws: WebSocket, reason: str, topic: Any) -> None:
    await _send(ws, {"type": "rejected", "reason": reason, "topic": topic})


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": detail}, status_code=status)


def create_app(
    settings: Settings | None = None,
    fetch: Callable[[str], Awaitable[Any]] = fetch_json,
) -> FastAPI:
    settings = settings or Settings.from_env()
    gateway = Gateway(
        settings=settings,
        catalog_config=load_catalog_config(),
        questionnaires=load_questionnaires(settings.questionnaires),
        store=ResponseStore(settings.data_dir),
        fetch=fetch,
    )

    app = FastAPI(title="HuRI playground", docs_url=None, redoc_url=None)
    app.state.gateway = gateway
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_page(request: Request, call_next: Any) -> Any:
        # The page and its assets change with every deploy; without this a
        # returning visitor's browser may pair a fresh page with stale JS/CSS.
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config() -> Dict[str, Any]:
        return await gateway.public_config()

    @app.post("/api/responses")
    async def responses(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            return _error(400, "body must be JSON")
        return gateway.submit(payload)

    @app.websocket("/ws")
    async def session(ws: WebSocket) -> None:
        await gateway.run_session(ws)

    return app
