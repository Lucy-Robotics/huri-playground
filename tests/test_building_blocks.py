import inspect
import json

import pytest
from huri_core.core.dataclasses.config import ClientConfig
from huri_core.core.plugins import PluginError, load_modules

from huri_playground.limits import RateLimiter, SessionSlots
from huri_playground.pipeline import RENDERED, InvalidPipeline, load_catalog_config
from huri_playground.questionnaire import InvalidAnswers, load_questionnaires
from huri_playground.store import AlreadyAnswered, ResponseStore, UnknownSession

from .curated import CURATED, details


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def installed_module(name):
    """The module class huri-core would load for `name`, or None when its
    package is not installed here (CI runs without the module packages)."""
    try:
        return load_modules([name])[name]
    except (ValueError, PluginError):
        return None


def test_module_args_match_what_huri_builds():
    """An arg HuRI's module does not take only fails once a visitor clicks
    start, deep in a Ray replica. Checked against the installed huri-module-*
    packages; a module whose package is not installed is skipped."""
    config = load_catalog_config()
    assert set(config.entries) <= set(CURATED)
    assert set(config.selected) <= set(config.entries)
    assert set(config.inbound) <= {topics[0] for topics in CURATED.values()}

    checked = 0
    for name, entry in config.entries.items():
        cls = installed_module(name)
        if cls is None:
            continue
        checked += 1
        assert (cls.input_type, cls.output_type) == CURATED[name], name
        params = inspect.signature(cls.__init__).parameters
        extra = [a for args in (entry.get("with") or {}).values() for a in args]
        extra += list(entry.get("options") or {})
        for arg in list(entry.get("args") or {}) + extra:
            assert arg in params, f"{name} has no argument {arg!r}"
    if checked == 0:
        pytest.skip("no huri-module-* package installed: nothing to check against")


def test_default_selection_is_a_valid_pipeline():
    catalog = load_catalog_config().catalog(details())
    pipeline = catalog.build(catalog.selected)
    config = ClientConfig.from_dict(pipeline.handshake("u"))
    assert set(config.modules) == set(catalog.selected)
    assert config.send == ["huri.question"]
    assert config.subscribe == ["huri.token", "huri.audio.out", "huri.motion"]
    assert config.modules["rag"].key == "rag"


def test_handshake_subscribes_only_to_what_the_page_renders():
    catalog = load_catalog_config().catalog(details())
    config = ClientConfig.from_dict(catalog.build(list(CURATED)).handshake("u"))
    assert config.subscribe == list(RENDERED)
    assert config.send == ["huri.question", "huri.audio.in"]
    assert "huri.voice" not in config.subscribe  # produced, but not shown


def test_unknown_module_gets_its_name_and_no_args():
    served = {"new": {"input": "huri.question", "output": "huri.token"}}
    catalog = load_catalog_config().catalog(served)
    assert catalog.public()["modules"] == [
        {
            "name": "new",
            "label": "new",
            "description": "",
            "group": "other",
            "input": "huri.question",
            "output": "huri.token",
            "options": {},
        }
    ]
    assert catalog.build(["new"]).modules == {"new": {"name": "new", "args": {}}}


def test_with_must_name_a_module(tmp_path):
    path = tmp_path / "modules.yaml"
    path.write_text(
        "inbound: {}\nmodules:\n  qag:\n    with: {nope: {use_emotion: true}}\n"
    )
    with pytest.raises(ValueError, match="nope"):
        load_catalog_config(path)


def test_invalid_pipeline_says_what_is_missing():
    catalog = load_catalog_config().catalog(details())
    with pytest.raises(InvalidPipeline, match="tts needs 'huri.token'"):
        catalog.build(["tts", "mov"])


def test_questionnaire_items():
    godspeed, rosas = load_questionnaires(["godspeed", "rosas"])
    assert (godspeed.points, len(godspeed.item_ids)) == (5, 23)
    assert (rosas.points, len(rosas.item_ids)) == (7, 18)


def test_every_item_feeds_a_score():
    for q in load_questionnaires(["godspeed", "rosas"]):
        assert set().union(*q.scores.values()) == set(q.item_ids), q.id


def test_godspeed_dimension_scores():
    [godspeed] = load_questionnaires(["godspeed"])
    assert list(godspeed.scores) == [
        "anthropomorphism",
        "animacy",
        "likeability",
        "perceived_intelligence",
        "perceived_safety",
    ]

    answers = {item: 1 for item in godspeed.item_ids}
    answers.update(like=5, friendly=5, kind=5, pleasant=5, nice=5, lifelike=4)
    scores = godspeed.score(answers)

    assert scores["likeability"] == 5.0
    # Artificial/Lifelike counts toward both of its dimensions.
    assert scores["anthropomorphism"] == round((4 + 1 * 4) / 5, 2)
    assert scores["animacy"] == round((4 + 1 * 5) / 6, 2)
    assert scores["perceived_safety"] == 1.0


def test_questionnaire_validation_returns_items_in_order():
    [rosas] = load_questionnaires(["rosas"])
    answers = {item: 7 for item in reversed(rosas.item_ids)}
    assert list(rosas.validate(answers)) == rosas.item_ids

    with pytest.raises(InvalidAnswers):
        rosas.validate([7] * len(rosas.item_ids))


def test_session_slots():
    slots = SessionSlots(2)
    assert slots.try_acquire() and slots.try_acquire()
    assert not slots.try_acquire()
    slots.release()
    assert slots.try_acquire()


def test_rate_limiter_window_slides():
    clock = Clock()
    limiter = RateLimiter(2, clock=clock)
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()

    clock.now = 59.9
    assert not limiter.allow()
    clock.now = 60.0
    assert limiter.allow()


def test_store_tickets_expire(tmp_path):
    clock = Clock(1000.0)
    store = ResponseStore(tmp_path, ticket_ttl_s=60, clock=clock)
    store.open_session("old", ["rag"])

    clock.now += 61
    with pytest.raises(UnknownSession):
        store.submit("old", {}, "")


def test_store_records_conversation_length(tmp_path):
    clock = Clock(1000.0)
    store = ResponseStore(tmp_path, clock=clock)
    record = store.open_session("s", ["rag"])
    record.questions = 4
    clock.now += 95
    store.close_session("s", "finished")
    clock.now += 300  # time spent on the questionnaire does not count

    store.submit("s", {"rosas": {"happy": 6}}, "")
    with pytest.raises(AlreadyAnswered):
        store.submit("s", {"rosas": {"happy": 6}}, "")

    entry = json.loads((tmp_path / "responses.jsonl").read_text())
    assert entry["conversation_seconds"] == 95.0
    assert entry["questions_asked"] == 4
    assert entry["answers"] == {"rosas": {"happy": 6}}


def test_module_with_no_group_goes_under_other():
    served = {**details(), "new": {"input": "huri.question", "output": "huri.token"}}
    public = load_catalog_config().catalog(served).public()
    assert [g["id"] for g in public["groups"]][-1] == "other"
    assert next(m for m in public["modules"] if m["name"] == "new")["group"] == "other"


def test_group_must_be_declared(tmp_path):
    path = tmp_path / "modules.yaml"
    path.write_text("inbound: {}\ngroups: {a: A}\nmodules:\n  rag: {group: b}\n")
    with pytest.raises(ValueError, match="'b'"):
        load_catalog_config(path)
