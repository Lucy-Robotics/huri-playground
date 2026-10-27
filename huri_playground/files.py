from pathlib import Path
from typing import Any, Dict, cast

from omegaconf import OmegaConf


def load_mapping(path: Path) -> Dict[str, Any]:
    """Read a yaml file whose top level is a mapping, as plain Python objects."""
    raw = OmegaConf.to_container(OmegaConf.load(path), resolve=True)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return cast(Dict[str, Any], raw)
