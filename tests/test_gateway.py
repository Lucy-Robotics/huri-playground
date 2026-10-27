"""The playground gateway, driven from a browser-side TestClient against a fake
HuRI. What matters here is what a public page must never get wrong: a visitor
picks module names among those HuRI runs but never their args, cannot reach
topics their pipeline does not consume, or exceed the session caps, and every
stored answer maps to one real conversation.

The fake HuRI stands in for the websocket the huri-core client SDK opens
(``websockets.connect`` is patched), so the SDK's handshake and framing are the
real ones.
"""

import asyncio
import json
import struct
import time

import pytest
import websockets
from fastapi.testclient import TestClient

from huri_playground.gateway import Settings, create_app
from huri_playground.questionnaire import load_questionnaires

from .curated import modules_reply

VOICE = ["vad", "stt", "tag", "qag", "rag", "tts"]


class FakeHuRI:
    """Answers each question with a short token stream, like RAG does.

    ``server_modules`` is what it lists on ``GET /modules`` (all by default).
    """

    def __init__(self, init_reply=None, answer="Hello there", server_modules=None):
        self.init_reply = init_reply or {"type": "session_init", "user_id": "x"}
        self.answer = answer
        self.server_modules = server_modules
        self.listed = 0
        self.sent = []
        self.handshakes = []
        self._out = None
        self._closed = None

    @property
    def out(self):
        # Created lazily: the gateway runs in TestClient's own event loop.
        if self._out is None:
            self._out = asyncio.Queue()
        return self._out

    @property
    def closed(self):
        if self._closed is None:
            self._closed = asyncio.Event()
        return self._closed

    @property
    def handshake(self):
        return self.handshakes[-1]

    def forwarded(self):
        return [m for m in self.sent if not _is_handshake(m)]

    # --- what the SDK's websocket connection does -------------------------

    async def send(self, msg):
        self.sent.append(msg)
        if _is_handshake(msg):
            self.handshakes.append(json.loads(msg))
            return
        if isinstance(msg, bytes) or self.answer is None:
            return
        for word in self.answer.split():
            token = {"topic": "huri.token", "data": {"text": word + " ", "end": False}}
            self.out.put_nowait(json.dumps(token))
        end = {"topic": "huri.token", "data": {"text": "", "end": True}}
        self.out.put_nowait(json.dumps(end))

    async def recv(self):
        return json.dumps(self.init_reply)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.out.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    async def close(self):
        if not self.closed.is_set():
            self.closed.set()
            self.out.put_nowait(None)

    async def wait_closed(self):
        await self.closed.wait()

    # --- GET /modules -----------------------------------------------------

    async def list_modules(self, url):
        assert url == "http://localhost:8000/modules"
        self.listed += 1
        return modules_reply(self.server_modules)


def _is_handshake(msg):
    return isinstance(msg, str) and '"user_id"' in msg


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """A TestClient of the gateway whose SDK client reaches the fake HuRI."""

    def make(huri=None, connect=None, fetch=None, **overrides):
        huri = huri or FakeHuRI()
        settings = Settings(data_dir=tmp_path, **overrides)

        async def fake_connect(url, **kwargs):
            assert url == settings.huri_url
            return huri

        monkeypatch.setattr(websockets, "connect", connect or fake_connect)
        app = create_app(settings, fetch=fetch or huri.list_modules)
        return TestClient(app), huri

    return make


def start(ws, modules=("rag",), access_code=""):
    ws.send_json(
        {"type": "start", "modules": list(modules), "access_code": access_code}
    )
    return ws.receive_json()


def mic_frame(payload=b"\x00" * 960, topic=b"huri.audio.in"):
    return struct.pack(">H", len(topic)) + topic + payload


def ask(ws, text):
    ws.send_json({"topic": "huri.question", "data": {"text": text}})


def receive(ws):
    """The next message from the gateway, past the clock's start."""
    msg = ws.receive_json()
    return ws.receive_json() if msg.get("type") == "clock" else msg


def read_answer(ws):
    words = []
    while True:
        msg = receive(ws)
        assert msg["topic"] == "huri.token", msg
        if msg["data"]["end"]:
            return "".join(words).strip()
        words.append(msg["data"]["text"])


def full_answers(value=3):
    return {
        q.id: {item_id: value for item_id in q.item_ids}
        for q in load_questionnaires(["godspeed", "rosas"])
    }


def stored(tmp_path):
    path = tmp_path / "responses.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_conversation_then_questionnaire(make_client, tmp_path):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        ready = start(ws)
        assert ready["type"] == "ready"
        session_id = ready["session_id"]

        ask(ws, "What is your name?")
        assert read_answer(ws) == "Hello there"

        ws.send_json({"type": "finish"})
        assert ws.receive_json() == {"type": "ended", "reason": "finished"}

    res = client.post(
        "/api/responses",
        json={"session_id": session_id, "answers": full_answers(), "comment": " ok "},
    )
    assert res.status_code == 201

    [entry] = stored(tmp_path)
    assert entry["session_id"] == session_id
    assert entry["modules"] == ["rag"]
    assert entry["questions_asked"] == 1
    assert entry["end_reason"] == "finished"
    assert entry["answers"] == full_answers()
    assert entry["scores"]["godspeed"]["likeability"] == 3.0
    assert set(entry["scores"]["rosas"]) == {"warmth", "competence", "discomfort"}
    assert entry["comment"] == "ok"
    # Ratings only: what the visitor said is not stored.
    assert "What is your name" not in json.dumps(entry)
    # The question reached HuRI as the SDK frames it.
    assert json.loads(huri.forwarded()[0]) == {
        "topic": "huri.question",
        "data": {"text": "What is your name?"},
    }


def test_visitors_pick_modules_but_never_their_args(make_client):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "start",
                "modules": ["rag"],
                "args": {"rag": {"tone": "rude"}},  # ignored
            }
        )
        assert ws.receive_json()["type"] == "ready"

    handshake = huri.handshake
    assert set(handshake) == {"user_id", "modules", "send", "subscribe"}
    assert handshake["modules"]["rag"]["name"] == "rag"
    assert handshake["modules"]["rag"]["args"]["tone"] == "friendly"
    assert handshake["user_id"].startswith("playground-")
    assert handshake["subscribe"] == ["huri.token"]
    assert handshake["send"] == ["huri.question"]


def test_voice_out_streams_huri_audio_and_gestures_to_the_browser(
    make_client, tmp_path
):
    client, huri = make_client()
    audio = mic_frame(
        struct.pack(">IBd", 22050, 1, 0.0) + b"\x00" * 16, b"huri.audio.out"
    )

    with client.websocket_connect("/ws") as ws:
        ready = start(ws, modules=["rag", "tts", "mov"])
        # What HuRI will stream: with huri.motion, the avatar gestures.
        assert ready["outbound"] == ["huri.token", "huri.audio.out", "huri.motion"]
        assert ready["modules"] == ["mov", "rag", "tts"]
        huri.out.put_nowait(audio)
        # Relayed byte for byte: the SDK decodes it as bytes and frames it again.
        assert ws.receive_bytes() == audio
        session_id = ready["session_id"]

    assert set(huri.handshake["modules"]) == {"rag", "tts", "mov"}
    # Which pipeline a visitor got is kept with the ratings: voice and gestures
    # change how lifelike HuRI seems.
    client.post(
        "/api/responses", json={"session_id": session_id, "answers": full_answers()}
    )
    assert stored(tmp_path)[0]["modules"] == ["mov", "rag", "tts"]


def test_microphone_frames_reach_huri(make_client):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        ready = start(ws, modules=VOICE)
        assert set(ready["inbound"]) == {"huri.question", "huri.audio.in"}
        assert ready["outbound"] == [
            "huri.token",
            "huri.audio.out",
            "huri.transcript",
            "huri.question",
        ]
        # No emotion module, so nothing on huri.emotion to show.

        ws.send_bytes(mic_frame())
        # Each topic keeps its own frame kind: HuRI hands huri.audio.in to the
        # VAD as raw bytes, and a question as JSON.
        ws.send_json({"topic": "huri.audio.in", "data": {"text": "hi"}})
        assert ws.receive_json()["reason"] == "bad_data"
        ws.send_bytes(mic_frame(b"hi", b"huri.question"))
        assert ws.receive_json()["reason"] == "bad_data"

    assert huri.forwarded() == [mic_frame()]
    handshake = huri.handshake
    assert set(handshake["send"]) == {"huri.question", "huri.audio.in"}
    assert handshake["modules"]["vad"]["args"]["block_duration"] == 0.03
    # No emotion module, so QAG must not wait for an emotion that never comes.
    assert handshake["modules"]["qag"]["args"]["use_emotion"] is False


def test_question_builder_waits_for_emotion_only_when_one_is_coming(make_client):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        assert start(ws, modules=VOICE + ["emo", "eag"])["type"] == "ready"

    assert huri.handshake["modules"]["qag"]["args"]["use_emotion"] is True
    # The page shows what HuRI senses from the voice.
    assert "huri.emotion" in huri.handshake["subscribe"]


@pytest.mark.parametrize(
    "modules",
    [
        [],
        "rag",
        [1],
        ["nope"],
        ["tts"],  # huri.token comes from rag
        ["qag", "rag"],  # huri.partial_question comes from tag or eag
        ["vad"],  # nothing the page shows
    ],
    ids=["none", "not-a-list", "not-names", "unknown", "unfed", "unfed-2", "unseen"],
)
def test_pipelines_that_cannot_run_are_refused(make_client, modules):
    client, huri = make_client()
    with client.websocket_connect("/ws") as ws:
        reply = start(ws, modules=modules)
    assert reply["type"] == "error"
    assert reply["reason"] == "invalid_pipeline"
    assert reply["detail"]
    assert huri.sent == []
    assert client.app.state.gateway.slots.in_use == 0


def start_with(ws, modules, options):
    ws.send_json({"type": "start", "modules": modules, "options": options})
    return ws.receive_json()


def test_emotion_and_answer_run_in_parallel_by_default(make_client, tmp_path):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        ready = start(ws, modules=VOICE + ["emo", "eag"])
        assert ready["options"] == {"qag": {"mode": "parallel"}}
        session_id = ready["session_id"]

    assert huri.handshake["modules"]["qag"]["args"]["mode"] == "parallel"
    client.post(
        "/api/responses", json={"session_id": session_id, "answers": full_answers()}
    )
    # Kept with the ratings: waiting for the emotion changes how HuRI feels.
    assert stored(tmp_path)[0]["options"] == {"qag": {"mode": "parallel"}}


def test_visitor_can_make_huri_wait_for_the_emotion(make_client):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        ready = start_with(ws, VOICE + ["emo", "eag"], {"qag": {"mode": "sequential"}})
        assert ready["options"] == {"qag": {"mode": "sequential"}}

    assert huri.handshake["modules"]["qag"]["args"]["mode"] == "sequential"


def test_mode_only_applies_when_an_emotion_is_coming(make_client):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        assert start(ws, modules=VOICE)["options"] == {}
    assert "mode" not in huri.handshake["modules"]["qag"]["args"]


@pytest.mark.parametrize(
    "options",
    [
        {"qag": {"mode": "both"}},  # not a listed value
        {"qag": {"use_emotion": False}},  # an arg, not an option
        {"rag": {"tone": "rude"}},
        {"qag": "sequential"},
        ["sequential"],
    ],
    ids=["value", "arg", "other-module", "not-a-mapping", "not-a-dict"],
)
def test_only_listed_option_values_are_accepted(make_client, options):
    client, huri = make_client()
    with client.websocket_connect("/ws") as ws:
        reply = start_with(ws, VOICE + ["emo", "eag"], options)
    assert reply["reason"] == "invalid_pipeline"
    assert huri.sent == []


def test_offered_modules_follow_what_huri_runs(make_client):
    # A machine where EMAGE cannot run: no gestures to pick.
    huri = FakeHuRI(server_modules={"rag", "tts"})
    client, _ = make_client(huri=huri)

    config = client.get("/api/config").json()
    assert [m["name"] for m in config["modules"]] == ["rag", "tts"]
    assert config["selected"] == ["rag", "tts"]

    with client.websocket_connect("/ws") as ws:
        reply = start(ws, modules=["rag", "tts", "mov"])
    assert reply["reason"] == "invalid_pipeline"
    assert "mov" in reply["detail"]


def test_playground_can_offer_fewer_modules_than_huri_runs(make_client):
    client, huri = make_client(modules=["rag", "tts"])
    config = client.get("/api/config").json()
    assert [m["name"] for m in config["modules"]] == ["rag", "tts"]

    with client.websocket_connect("/ws") as ws:
        assert start(ws, modules=["vad", "stt", "tag", "qag", "rag"])["reason"] == (
            "invalid_pipeline"
        )


def test_huri_module_list_is_cached_briefly(make_client):
    client, huri = make_client()
    gateway = client.app.state.gateway
    now = [0.0]
    gateway.clock = lambda: now[0]

    client.get("/api/config")
    client.get("/api/config")
    assert huri.listed == 1

    # A HuRI redeployed with other modules shows up without a restart.
    huri.server_modules = {"rag"}
    now[0] += 11
    assert [m["name"] for m in client.get("/api/config").json()["modules"]] == ["rag"]
    assert huri.listed == 2


def test_huri_down_when_the_page_loads(make_client):
    async def down(url):
        raise ConnectionRefusedError("no HuRI")

    client, huri = make_client(fetch=down)
    config = client.get("/api/config").json()
    assert config["huri_available"] is False
    assert config["modules"] == []

    with client.websocket_connect("/ws") as ws:
        assert start(ws) == {"type": "error", "reason": "huri_unavailable"}
    assert huri.sent == []


def test_an_old_huri_without_module_details_counts_as_down(make_client):
    async def old(url):
        return {"modules": ["rag"]}  # no "details": a huri-core before 0.4

    client, huri = make_client(fetch=old)
    assert client.get("/api/config").json()["huri_available"] is False


def test_each_visit_gets_its_own_user_id(make_client):
    client, huri = make_client()
    ids = []
    for _ in range(2):
        with client.websocket_connect("/ws") as ws:
            start(ws)
        ids.append(json.loads(huri.sent[-1])["user_id"])
    assert ids[0] != ids[1]


def test_answers_can_only_be_submitted_once(make_client, tmp_path):
    client, _ = make_client()
    with client.websocket_connect("/ws") as ws:
        session_id = start(ws)["session_id"]

    body = {"session_id": session_id, "answers": full_answers()}
    assert client.post("/api/responses", json=body).status_code == 201
    assert client.post("/api/responses", json=body).status_code == 409
    assert len(stored(tmp_path)) == 1


def test_answers_need_a_real_session(make_client, tmp_path):
    client, _ = make_client()
    res = client.post(
        "/api/responses", json={"session_id": "made-up", "answers": full_answers()}
    )
    assert res.status_code == 404
    assert stored(tmp_path) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a["godspeed"].pop("kind"),
        lambda a: a.pop("rosas"),
        lambda a: a["rosas"].update(scary=8),
        lambda a: a["godspeed"].update(kind=0),
        lambda a: a["godspeed"].update(kind=True),
        lambda a: a["godspeed"].update(kind="3"),
        lambda a: a["rosas"].update(extra=3),
    ],
    ids=["missing", "no-rosas", "too-high", "too-low", "bool", "string", "unknown"],
)
def test_invalid_answers_are_refused(make_client, tmp_path, mutate):
    client, _ = make_client()
    with client.websocket_connect("/ws") as ws:
        session_id = start(ws)["session_id"]

    answers = full_answers()
    mutate(answers)
    res = client.post(
        "/api/responses", json={"session_id": session_id, "answers": answers}
    )
    assert res.status_code == 400
    assert stored(tmp_path) == []


def test_access_code(make_client):
    client, _ = make_client(access_code="lucy")
    assert client.get("/api/config").json()["access_code_required"] is True

    with client.websocket_connect("/ws") as ws:
        assert start(ws, access_code="nope") == {
            "type": "error",
            "reason": "bad_access_code",
        }
    with client.websocket_connect("/ws") as ws:
        assert start(ws, access_code="lucy")["type"] == "ready"


def test_busy_when_all_slots_are_taken(make_client):
    client, _ = make_client(max_sessions=1)

    with client.websocket_connect("/ws") as first:
        assert start(first)["type"] == "ready"
        with client.websocket_connect("/ws") as second:
            assert start(second) == {"type": "error", "reason": "busy"}
        first.send_json({"type": "finish"})
        assert first.receive_json()["reason"] == "finished"

    # The slot is free again once the first visitor is done.
    with client.websocket_connect("/ws") as third:
        assert start(third)["type"] == "ready"


def test_huri_unreachable(make_client):
    async def refused(url, **kwargs):
        raise ConnectionRefusedError("no HuRI")

    client, _ = make_client(connect=refused)
    with client.websocket_connect("/ws") as ws:
        assert start(ws) == {"type": "error", "reason": "huri_unavailable"}

    # A failed start must not leak the session slot.
    assert client.app.state.gateway.slots.in_use == 0


def test_huri_rejects_the_session(make_client):
    huri = FakeHuRI(
        init_reply={
            "type": "session_error",
            "error": "no rag",
            "server_modules": ["tag"],
        }
    )
    client, _ = make_client(huri=huri)
    with client.websocket_connect("/ws") as ws:
        assert start(ws) == {"type": "error", "reason": "huri_rejected"}
    assert client.app.state.gateway.slots.in_use == 0


def test_huri_closing_the_session_ends_the_conversation(make_client):
    client, huri = make_client()
    with client.websocket_connect("/ws") as ws:
        start(ws)
        huri.out.put_nowait(None)  # HuRI hangs up
        huri.closed.set()
        assert ws.receive_json() == {"type": "ended", "reason": "huri_closed"}


def test_topics_outside_the_preset_are_dropped(make_client):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        start(ws)
        ws.send_json({"topic": "huri.token", "data": {"text": "spoof", "end": False}})
        assert ws.receive_json() == {
            "type": "rejected",
            "reason": "topic_not_allowed",
            "topic": "huri.token",
        }

        topic = b"huri.audio.in"
        ws.send_bytes(struct.pack(">H", len(topic)) + topic + b"\x00" * 64)
        assert ws.receive_json()["reason"] == "topic_not_allowed"

    assert huri.forwarded() == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_empty_or_long_questions_are_dropped(make_client, text):
    client, huri = make_client()

    with client.websocket_connect("/ws") as ws:
        start(ws)
        ask(ws, text)
        assert ws.receive_json()["reason"] == "bad_data"

    assert huri.forwarded() == []


def test_questions_are_rate_limited(make_client):
    client, huri = make_client(huri=FakeHuRI(answer=None))

    with client.websocket_connect("/ws") as ws:
        start(ws)
        for i in range(10):
            ask(ws, f"question {i}")
        ask(ws, "one too many")
        # A frame that is always rejected, so a missing rate limit fails the
        # test instead of hanging it on a reply that never comes.
        ws.send_json({"topic": "sentinel", "data": {}})
        assert receive(ws) == {
            "type": "rejected",
            "reason": "rate_limited",
            "topic": "huri.question",
        }
        assert ws.receive_json()["topic"] == "sentinel"

    assert len(huri.forwarded()) == 10


def test_session_time_counts_from_the_first_words(make_client, tmp_path):
    client, _ = make_client(huri=FakeHuRI(answer=None), session_seconds=0.2)

    with client.websocket_connect("/ws") as ws:
        session_id = start(ws)["session_id"]
        time.sleep(0.4)  # longer than the session: its time has not started
        ask(ws, "hello")
        assert ws.receive_json() == {"type": "clock", "max_seconds": 0.2}
        assert ws.receive_json() == {"type": "ended", "reason": "time_limit"}

    # Running out of time still leads to the questionnaire.
    res = client.post(
        "/api/responses", json={"session_id": session_id, "answers": full_answers()}
    )
    assert res.status_code == 201
    assert stored(tmp_path)[0]["end_reason"] == "time_limit"


class HearingHuRI(FakeHuRI):
    """Hears words in any audio it is sent."""

    async def send(self, msg):
        await super().send(msg)
        if isinstance(msg, bytes):
            heard = {"topic": "huri.transcript", "data": {"text": "hi", "end": True}}
            self.out.put_nowait(json.dumps(heard))


def test_huri_hearing_words_starts_the_time(make_client):
    """Speech reaches HuRI as audio: its first event shows it heard words."""
    client, _ = make_client(huri=HearingHuRI(), session_seconds=0.2)

    with client.websocket_connect("/ws") as ws:
        assert start(ws, modules=VOICE)["type"] == "ready"
        ws.send_bytes(mic_frame())
        first = ws.receive_json()
        if first.get("type") == "clock":  # the clock and the event race
            first = ws.receive_json()
        else:
            assert ws.receive_json()["type"] == "clock"
        assert first["topic"] == "huri.transcript"
        assert ws.receive_json() == {"type": "ended", "reason": "time_limit"}


def test_a_session_nobody_speaks_in_closes(make_client, tmp_path):
    client, _ = make_client(session_seconds=60, idle_seconds=0.2)

    with client.websocket_connect("/ws") as ws:
        start(ws)
        assert ws.receive_json() == {"type": "ended", "reason": "idle"}

    assert stored(tmp_path) == [] or stored(tmp_path)[0]["end_reason"] == "idle"


def test_public_config_lists_modules_but_not_their_args(make_client):
    client, _ = make_client()
    config = client.get("/api/config").json()

    assert config["access_code_required"] is False
    assert config["huri_available"] is True
    # In the flow's order, not HuRI's alphabetical one.
    assert [m["name"] for m in config["modules"]] == [
        "vad",
        "stt",
        "tag",
        "emo",
        "eag",
        "qag",
        "rag",
        "tts",
        "mov",
    ]
    for module in config["modules"]:
        assert set(module) == {
            "name",
            "label",
            "description",
            "group",
            "input",
            "output",
            "options",
        }
    qag = next(m for m in config["modules"] if m["name"] == "qag")
    assert qag["options"]["mode"]["default"] == "parallel"
    assert [v["value"] for v in qag["options"]["mode"]["values"]] == [
        "parallel",
        "sequential",
    ]
    assert "max_history_turns" not in json.dumps(config)
    assert config["inbound"] == ["huri.question", "huri.audio.in"]
    assert config["rendered"] == [
        "huri.token",
        "huri.audio.out",
        "huri.motion",
        "huri.transcript",
        "huri.question",
        "huri.emotion",
    ]
    assert config["selected"] == ["rag", "tts", "mov"]
    assert [g["id"] for g in config["groups"]] == ["listen", "feel", "think", "speak"]
    assert [q["id"] for q in config["questionnaires"]] == ["godspeed", "rosas"]


def test_modules_url_is_derived_from_the_session_url():
    assert (
        Settings(huri_url="wss://huri.example/api/session").huri_modules_url
        == "https://huri.example/api/modules"
    )


def test_page_is_served(make_client):
    client, _ = make_client()
    res = client.get("/")
    assert res.status_code == 200
    assert "/static/app.js" in res.text
    asset = client.get("/static/app.js")
    assert asset.status_code == 200
    # Browsers must revalidate, or a deploy leaves visitors on stale JS/CSS.
    assert res.headers["cache-control"] == asset.headers["cache-control"]
    assert res.headers["cache-control"] == "no-cache"
