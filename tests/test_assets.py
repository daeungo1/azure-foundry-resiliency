"""Validate persistent guide links, accessible SVGs and generated configuration shape."""

import re
from pathlib import Path
from xml.etree import ElementTree

import pytest

from foundry_resiliency.config import TARGET_MODELS, load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["same", "mixed"])
def test_persistent_example_configuration(mode: str) -> None:
    config = load_settings(ROOT / "configs" / f"{mode}-model.example.json")
    assert len(config.backends) == 16
    assert {backend.model: backend.version for backend in config.backends} == TARGET_MODELS
    assert {backend.region for backend in config.backends} == {
        "koreacentral",
        "uksouth",
        "eastus2",
        "canadaeast",
    }


def test_svgs_are_accessible_static_and_self_contained() -> None:
    diagrams = list((ROOT / "docs" / "diagrams").glob("*.svg"))
    assert len(diagrams) == 4
    for path in diagrams:
        root = ElementTree.parse(path).getroot()
        assert root.attrib["viewBox"]
        assert root.attrib["role"] == "img"
        assert root.find("{http://www.w3.org/2000/svg}title") is not None
        assert root.find("{http://www.w3.org/2000/svg}desc") is not None
        text = path.read_text(encoding="utf-8")
        assert "<script" not in text
        assert "href=" not in text


def test_markdown_local_links_resolve() -> None:
    documents = [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]
    missing = []
    for path in documents:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in link or link.startswith("#"):
                continue
            target = path.parent / link.split("#")[0]
            if not target.exists():
                missing.append(f"{path.name}: {link}")
    assert not missing, missing
