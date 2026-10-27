"""What huri-modules' curated modules consume and emit, as HuRI's
``GET /modules`` reports them. The real classes are not needed to test the
gateway, which only reads topics; test_building_blocks checks this table
against the installed packages when they are present."""

from typing import Any, Dict, Iterable, Optional

CURATED = {
    "vad": ("huri.audio.in", "huri.voice"),
    "stt": ("huri.voice", "huri.transcript"),
    "tag": ("huri.transcript", "huri.partial_question"),
    "emo": ("huri.voice", "huri.emotion"),
    "eag": ("huri.emotion", "huri.partial_question"),
    "qag": ("huri.partial_question", "huri.question"),
    "rag": ("huri.question", "huri.token"),
    "tts": ("huri.token", "huri.audio.out"),
    "mov": ("huri.audio.out", "huri.motion"),
}


def details(names: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """The ``details`` of ``GET /modules``, for all or some curated modules."""
    wanted = set(CURATED) if names is None else set(names)
    return {
        name: {"input": topics[0], "output": topics[1], "actuation": False}
        for name, topics in sorted(CURATED.items())
        if name in wanted
    }


def modules_reply(names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """A whole ``GET /modules`` answer."""
    found = details(names)
    return {"modules": sorted(found), "details": found, "huri_core": "0.4.0"}
