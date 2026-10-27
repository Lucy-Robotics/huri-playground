"""scripts/check_modules_yaml.py: modules.yaml only names registered modules."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "check_modules_yaml", ROOT / "scripts" / "check_modules_yaml.py"
)
assert spec is not None and spec.loader is not None
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

REGISTRY = """
schema = 2

[[module]]
name = "rag"
package = "huri-module-rag"

[[module]]
name = "tts"
package = "huri-module-tts"
"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_the_shipped_modules_yaml_passes_against_a_registry_listing_them(tmp_path):
    names = "".join(
        f'\n[[module]]\nname = "{n}"\npackage = "p"\n'
        for n in ["vad", "stt", "tag", "emo", "eag", "qag", "rag", "tts", "mov"]
    )
    registry = write(tmp_path, "registry.toml", "schema = 2\n" + names)
    assert check.main([str(registry)]) == 0


def test_an_unknown_module_fails(tmp_path, capsys):
    registry = write(tmp_path, "registry.toml", REGISTRY)
    config = write(
        tmp_path,
        "modules.yaml",
        "inbound: {}\nselected: [rag, nope]\nmodules:\n  rag: {}\n  gesture: {}\n",
    )
    assert check.main([str(registry), str(config)]) == 1
    err = capsys.readouterr().err
    assert "modules.gesture = 'gesture'" in err
    assert "selected[1] = 'nope'" in err
    assert "rag" not in err.replace("registry.toml", "")


def test_usage_errors(tmp_path):
    assert check.main([]) == 2
    empty = write(tmp_path, "registry.toml", "schema = 2\n")
    assert check.main([str(empty)]) == 2
