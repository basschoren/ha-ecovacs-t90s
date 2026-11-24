"""Translations, services.yaml and icons are consistent."""

import json
import pathlib
import re
from typing import Any

import yaml

COMP = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "ecovacs_t90s"
)


def _load(name: str) -> Any:
    return json.loads((COMP / name).read_text(encoding="utf-8"))


def _flat(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def test_languages_in_sync() -> None:
    """strings.json == en.json; en and nl have the same keys and placeholders."""
    en = _load("translations/en.json")
    assert _load("strings.json") == en
    flat_en, flat_nl = _flat(en), _flat(_load("translations/nl.json"))
    assert set(flat_en) == set(flat_nl)
    for key, text in flat_en.items():
        placeholders = set(re.findall(r"\{(\w+)\}", text))
        assert placeholders == set(re.findall(r"\{(\w+)\}", flat_nl[key])), key


def test_services_and_icons_translated() -> None:
    """Every service field and every icon has a translation."""
    en = _load("translations/en.json")
    services = yaml.safe_load((COMP / "services.yaml").read_text(encoding="utf-8"))
    assert set(services) == set(en["services"])
    for name, spec in services.items():
        for field in (spec or {}).get("fields", {}):
            assert en["services"][name]["fields"][field]["name"], (name, field)
    icons = _load("icons.json")
    assert set(icons["services"]) == set(services)
    for platform, keys in icons["entity"].items():
        for key in keys:
            assert key in en["entity"][platform], (platform, key)


def test_manifest() -> None:
    """Version and the optional Matter ordering."""
    manifest = _load("manifest.json")
    assert manifest["version"] == "1.0.0"
    assert manifest["after_dependencies"] == ["matter"]
    assert "dependencies" not in manifest
