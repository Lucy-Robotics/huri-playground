"""Check that modules.yaml only names modules HuRI's registry lists.

    python scripts/check_modules_yaml.py <registry.toml> [modules.yaml]

<registry.toml> is huri-modules' registry of curated modules (CI downloads it
at HURI_MODULES_REF). Every key of `modules:` and every name in `selected:`
must be the `name` of a `[[module]]` entry there. The list a visitor sees
still comes from the server (GET /modules): this only catches a typo, or a
module renamed in huri-modules, before a visitor does. No network: the file is
read with OmegaConf, which huri-core[client] brings.
Exits 1 on an unknown name, 2 on bad usage.
"""

import sys
from pathlib import Path
from typing import Iterable, List, Set

from omegaconf import OmegaConf

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
MODULES_YAML = ROOT / "huri_playground" / "modules.yaml"


def registry_names(path: Path) -> Set[str]:
    with open(path, "rb") as f:
        registry = tomllib.load(f)
    return {entry["name"] for entry in registry.get("module", [])}


def unknown_modules(config: Path, known: Set[str]) -> List[str]:
    raw = OmegaConf.to_container(OmegaConf.load(config), resolve=False)
    assert isinstance(raw, dict)
    named = [f"modules.{name}" for name in raw.get("modules") or {}]
    named += [f"selected[{i}]" for i, _ in enumerate(raw.get("selected") or [])]
    values = list(raw.get("modules") or {}) + list(raw.get("selected") or [])
    return [
        f"{config.name}: {where} = {name!r}"
        for where, name in zip(named, values)
        if name not in known
    ]


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    registry = Path(args[0])
    config = Path(args[1]) if len(args) > 1 else MODULES_YAML
    known = registry_names(registry)
    if not known:
        print(f"{registry}: no [[module]] entry", file=sys.stderr)
        return 2
    bad = unknown_modules(config, known)
    for line in bad:
        print(f"not in {registry.name}: {line}", file=sys.stderr)
    if not bad:
        print(f"ok: {config.name} against {len(known)} registered modules")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
