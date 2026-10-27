"""Turn the modules a visitor ticked into a HuRI session.

HuRI wires a session by topic: each module consumes one topic and may emit
one, and a module whose topic never arrives just sits there. The browser can
publish only a few topics (typed questions, microphone frames) and render only
a few, so a selection is accepted when every module's input comes either from
the browser or from another selected module, the browser can reach at least
one of them, and something they produce shows up on the page.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from .files import load_mapping

MODULES_PATH = Path(__file__).parent / "modules.yaml"

# Where a module goes when modules.yaml gives it no group.
OTHER_GROUP = "other"

# Topics static/app.js knows how to show or play, in the order the page lists
# them, with their frame kind on the wire (huri-core's client SDK sends a JSON
# event as a text frame and a bytes event as a binary frame). HuRI refuses a
# subscription to a topic no module of the session emits, so only the produced
# ones are subscribed to.
RENDERED: Dict[str, str] = {
    "huri.token": "json",
    "huri.audio.out": "binary",
    "huri.motion": "binary",
    "huri.transcript": "json",
    "huri.question": "json",
    "huri.emotion": "json",
}


class InvalidPipeline(ValueError):
    pass


@dataclass(frozen=True)
class InboundRule:
    """Limits on one topic the browser is allowed to publish."""

    max_per_minute: int
    max_chars: int | None = None
    # Binary frames ([u16 topic_len][topic][payload]) instead of JSON.
    binary: bool = False


@dataclass(frozen=True)
class Option:
    """One arg a visitor may set, to one of a few values listed in modules.yaml."""

    label: str
    default: str
    # Value -> how the page describes it.
    values: Mapping[str, str]
    # Only offered, and only set, when this module is in the pipeline too.
    needs: str | None = None
    # Modules the page ticks when a value is picked, so it applies at once.
    ticks: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "default": self.default,
            "values": [{"value": v, "label": text} for v, text in self.values.items()],
            "needs": self.needs,
            "ticks": self.ticks,
        }


@dataclass(frozen=True)
class ModuleInfo:
    """A module HuRI runs, with what the playground adds to it."""

    name: str
    input: str
    output: str | None
    label: str
    description: str
    group: str
    args: Mapping[str, Any]
    # Other module -> args added when both are in the pipeline.
    with_args: Mapping[str, Mapping[str, Any]]
    options: Mapping[str, Option] = field(default_factory=dict)

    def public(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "group": self.group,
            "input": self.input,
            "output": self.output,
            "options": {name: o.public() for name, o in self.options.items()},
        }


@dataclass(frozen=True)
class Pipeline:
    """A checked selection: what to ask HuRI for and what may cross the gateway."""

    modules: Mapping[str, Any]
    inbound: Mapping[str, InboundRule]
    # Topics HuRI streams back, in RENDERED's order.
    outbound: List[str]
    # Module -> option -> value, for the options this pipeline offers.
    options: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def handshake(self, user_id: str) -> Dict[str, Any]:
        """The session HuRI expects as the first message on ``/session``: the
        ``ClientConfig`` of huri-core (``user_id``, ``modules``, the topics the
        client ``send``s and those it ``subscribe``s to)."""
        return {
            "user_id": user_id,
            "modules": dict(self.modules),
            "send": list(self.inbound),
            "subscribe": list(self.outbound),
        }


class Catalog:
    """The modules a visitor can pick from right now."""

    def __init__(
        self,
        modules: Iterable[ModuleInfo],
        inbound: Mapping[str, InboundRule],
        selected: Iterable[str],
        groups: Mapping[str, str] | None = None,
    ):
        self.modules = {m.name: m for m in modules}
        self.inbound = dict(inbound)
        self.selected = [name for name in selected if name in self.modules]
        self.groups = dict(groups or {})

    def public(self) -> Dict[str, Any]:
        used = {m.group for m in self.modules.values()}
        groups = {**self.groups, OTHER_GROUP: "Other"}
        return {
            "groups": [
                {"id": g, "label": label} for g, label in groups.items() if g in used
            ],
            "modules": [m.public() for m in self.modules.values()],
            "inbound": list(self.inbound),
            "rendered": list(RENDERED),
            "selected": self.selected,
        }

    def build(self, names: Any, picked: Any = None) -> Pipeline:
        """``picked``: the visitor's option values, ``{module: {option: value}}``."""
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) for n in names)
        ):
            raise InvalidPipeline("pick at least one module")
        unknown = sorted(set(names) - set(self.modules))
        if unknown:
            raise InvalidPipeline(f"unknown module(s): {', '.join(unknown)}")

        chosen = [self.modules[n] for n in dict.fromkeys(names)]
        produced = {m.output for m in chosen if m.output is not None}
        for m in chosen:
            if m.input not in produced and m.input not in self.inbound:
                raise InvalidPipeline(
                    f"{m.name} needs {m.input!r}, which no selected module produces"
                )

        consumed = {m.input for m in chosen}
        inbound = {t: rule for t, rule in self.inbound.items() if t in consumed}
        if not inbound:
            raise InvalidPipeline("nothing you can type or say reaches these modules")
        outbound = [t for t in RENDERED if t in produced]
        if not outbound:
            raise InvalidPipeline("these modules produce nothing the page can show")

        selected = {m.name for m in chosen}
        options = self._options(chosen, selected, picked)
        modules = {}
        for m in chosen:
            args = dict(m.args)
            for other, extra in m.with_args.items():
                if other in selected:
                    args.update(extra)
            args.update(options.get(m.name, {}))
            modules[m.name] = {"name": m.name, "args": args}
        return Pipeline(
            modules=modules, inbound=inbound, outbound=outbound, options=options
        )

    @staticmethod
    def _options(
        chosen: List[ModuleInfo], selected: set, picked: Any
    ) -> Dict[str, Dict[str, str]]:
        picked = picked if picked is not None else {}
        if not isinstance(picked, dict) or not all(
            isinstance(v, dict) for v in picked.values()
        ):
            raise InvalidPipeline("options must map modules to {option: value}")
        offered = {m.name: m.options for m in chosen if m.options}
        for name, values in picked.items():
            for option, value in values.items():
                spec = offered.get(name, {}).get(option)
                if spec is None:
                    raise InvalidPipeline(f"{name} has no option {option!r} here")
                if value not in spec.values:
                    raise InvalidPipeline(f"{name}.{option} cannot be {value!r}")

        result: Dict[str, Dict[str, str]] = {}
        for name, specs in offered.items():
            for option, spec in specs.items():
                if spec.needs is not None and spec.needs not in selected:
                    continue
                value = picked.get(name, {}).get(option, spec.default)
                result.setdefault(name, {})[option] = value
        return result


@dataclass(frozen=True)
class CatalogConfig:
    """modules.yaml: how to present and configure the modules HuRI reports."""

    inbound: Mapping[str, InboundRule]
    selected: List[str]
    entries: Mapping[str, Mapping[str, Any]]
    groups: Mapping[str, str] = field(default_factory=dict)

    def catalog(
        self,
        details: Mapping[str, Mapping[str, Any]],
        allowed: Iterable[str] | None = None,
    ) -> Catalog:
        """Combine HuRI's ``GET /modules`` with this file.

        ``details`` is that answer's ``details`` key: each enabled module's
        ``input`` and ``output`` topics (``output`` is null for a terminal
        module). ``allowed`` narrows what visitors see (``PLAYGROUND_MODULES``).
        Modules come in this file's order, which follows the flow from
        microphone to answer; ones it does not list go last.
        """
        allowed = set(allowed) if allowed is not None else None
        order = {name: i for i, name in enumerate(self.entries)}
        modules = []
        for name in sorted(details, key=lambda n: order.get(n, len(order))):
            if allowed is not None and name not in allowed:
                continue
            item = details[name]
            entry = self.entries.get(name, {})
            modules.append(
                ModuleInfo(
                    name=name,
                    input=item["input"],
                    output=item.get("output"),
                    label=entry.get("label", name),
                    description=entry.get("description", ""),
                    group=entry.get("group") or OTHER_GROUP,
                    args=entry.get("args") or {},
                    with_args=entry.get("with") or {},
                    options={
                        option: Option(**spec)
                        for option, spec in (entry.get("options") or {}).items()
                    },
                )
            )
        return Catalog(modules, self.inbound, self.selected, self.groups)


def load_catalog_config(path: Path = MODULES_PATH) -> CatalogConfig:
    raw = load_mapping(path)
    entries = raw.get("modules") or {}
    groups = raw.get("groups") or {}
    for name, entry in entries.items():
        if entry.get("group") is not None and entry["group"] not in groups:
            raise ValueError(
                f"{path}: {name}.group {entry['group']!r} is not in groups"
            )
        for other in entry.get("with") or {}:
            if other not in entries:
                raise ValueError(f"{path}: {name}.with names unknown module {other!r}")
        for option, spec in (entry.get("options") or {}).items():
            if spec["default"] not in spec["values"]:
                raise ValueError(f"{path}: {name}.{option} default is not a value")
            for other in [spec.get("needs"), *spec.get("ticks", [])]:
                if other is not None and other not in entries:
                    raise ValueError(f"{path}: {name}.{option} names unknown {other!r}")
    return CatalogConfig(
        inbound={topic: InboundRule(**rule) for topic, rule in raw["inbound"].items()},
        selected=list(raw.get("selected") or []),
        entries=entries,
        groups=groups,
    )
