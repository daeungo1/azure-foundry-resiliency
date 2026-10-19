"""Validate persistent guide links, accessible SVGs and generated configuration shape."""

import json
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
        "Region01",
        "Region02",
        "Region03",
        "Region04",
    }
    assert all(backend.azure_location is None for backend in config.backends)


def test_shared_assets_do_not_expose_deployment_geography() -> None:
    country_codes = [
        "".join(pair) for pair in [("K", "R"), ("U", "K"), ("U", "S"), ("C", "A"), ("E", "U")]
    ]
    locations = [
        "".join(parts)
        for parts in [
            ("korea", "central"),
            ("uk", "south"),
            ("east", "us2"),
            ("canada", "east"),
            ("sweden", "central"),
        ]
    ]
    forbidden = re.compile(
        r"(?<![A-Za-z])(?:" + "|".join(country_codes + locations) + r")(?![A-Za-z])", re.I
    )
    paths = [
        ROOT / "README.md",
        *(ROOT / "docs").rglob("*"),
        *(ROOT / "configs").glob("*.example.json"),
        ROOT / "infra" / "main.bicepparam",
    ]
    leaks = [
        str(path.relative_to(ROOT))
        for path in paths
        if path.is_file() and forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert not leaks, leaks


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


@pytest.mark.parametrize("protocol", ["http", "sse"])
def test_persisted_evidence_contains_verified_outputs_and_real_attempts(protocol: str) -> None:
    report = json.loads(
        (ROOT / "docs" / "results" / f"resilience-{protocol}.json").read_text(encoding="utf-8")
    )
    assert report["passed"] and report["live_azure_calls"] == 0
    assert report["timing_mode"] == "virtual_policy_clock"
    assert len(report["scenarios"]) == 80
    assert sum(row["requests"] for row in report["scenarios"]) == 800
    assert sum(row["successes"] for row in report["scenarios"]) == 484
    assert sum(row["failures"] for row in report["scenarios"]) == 316
    for row in report["scenarios"]:
        assert row["passed"]
        assert len(row["samples"]) == row["requests"]
        assert sum(sample["success"] for sample in row["samples"]) == row["successes"]
        assert row["http_attempts"] == sum(len(sample["attempts"]) for sample in row["samples"])
        for sample in row["samples"]:
            assert all(re.fullmatch(r"Region0[1-4]", step["region"]) for step in sample["attempts"])
            if sample["success"]:
                assert sample["output_verified"]
                assert sample["attempts"][-1]["status"] == 200
                assert sample["attempts"][-1]["model"] == sample["selected_model"]
            else:
                assert sample["failure_reason"]
        if row["scenario"] in {"same_model_429", "same_model_503"}:
            first = row["samples"][0]
            expected_status = 429 if row["scenario"] == "same_model_429" else 503
            assert [step["status"] for step in first["attempts"][:4]] == [expected_status] * 4
            if row["routing_mode"] == "mixed":
                assert row["degraded_responses"] == 10
                assert first["selected_model"] != row["requested_model"]
                assert len(first["attempts"]) == 5
                assert all(len(sample["attempts"]) == 1 for sample in row["samples"][1:])


def test_readme_success_tables_match_persisted_evidence() -> None:
    rows = [
        row
        for protocol in ("http", "sse")
        for row in json.loads(
            (ROOT / "docs" / "results" / f"resilience-{protocol}.json").read_text(encoding="utf-8")
        )["scenarios"]
    ]
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    table = {
        cells[0].strip("`"): cells[1:]
        for line in text.splitlines()
        if line.startswith("| `")
        if (cells := [cell.strip() for cell in line.split("|")[1:-1]])
    }
    for scenario in {row["scenario"] for row in rows}:
        for index, mode in enumerate(("same", "mixed")):
            selected = [
                row for row in rows if row["scenario"] == scenario and row["routing_mode"] == mode
            ]
            expected = (
                f"{sum(row['successes'] for row in selected)}/"
                f"{sum(row['requests'] for row in selected)}"
            )
            assert table[scenario][index] == expected
    for model in TARGET_MODELS:
        model_cells = next(
            [cell.strip() for cell in line.split("|")[1:-1]]
            for line in text.splitlines()
            if line.startswith(f"| {model} ") and "20/20" in line
        )
        for index, scenario in enumerate(
            ("same_model_429", "same_model_503", "fallback_model_429")
        ):
            selected = [
                row
                for row in rows
                if row["requested_model"] == model
                and row["scenario"] == scenario
                and row["routing_mode"] == "mixed"
            ]
            assert model_cells[index + 1] == (
                f"{sum(row['successes'] for row in selected)}/"
                f"{sum(row['requests'] for row in selected)}"
            )


@pytest.mark.parametrize("protocol", ["http", "sse"])
def test_persisted_two_subscription_evidence(protocol: str) -> None:
    report = json.loads(
        (ROOT / "docs" / "results" / f"two-subscriptions-{protocol}.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["passed"] and report["live_azure_calls"] == 0
    assert report["timing_mode"] == "virtual_policy_clock"
    assert report["topology"]["subscriptions"] == ["Subscription01", "Subscription02"]
    assert report["topology"]["backend_count"] == 32
    assert report["topology"]["quota_pool_count"] == 8
    assert not report["topology"]["capacity_isolation_verified"]
    assert len(report["scenarios"]) == 112
    assert sum(row["requests"] for row in report["scenarios"]) == 1120
    assert sum(row["successes"] for row in report["scenarios"]) == 840
    assert sum(row["failures"] for row in report["scenarios"]) == 280
    for row in report["scenarios"]:
        assert row["passed"] and row["model_invariant"]
        assert row["http_attempts"] == sum(len(sample["attempts"]) for sample in row["samples"])
        assert sum(sample["success"] for sample in row["samples"]) == row["successes"]
        for sample in row["samples"]:
            assert len(sample["attempts"]) <= 20
            if sample["success"]:
                assert sample["output_verified"]
                assert sample["attempts"][-1]["subscription"] == sample["selected_subscription"]
                assert sample["attempts"][-1]["status"] == 200
        if row["scenario"] in {"subscription_request_headroom", "subscription_token_headroom"}:
            assert row["successes"] == 10 and row["degraded_responses"] == 0
            assert row["samples"][0]["selected_subscription"] == "Subscription01"
            assert all(
                sample["selected_subscription"] == "Subscription02" and len(sample["attempts"]) == 1
                for sample in row["samples"][1:]
            )


def test_two_subscription_readme_table_matches_evidence() -> None:
    rows = [
        row
        for protocol in ("http", "sse")
        for row in json.loads(
            (ROOT / "docs" / "results" / f"two-subscriptions-{protocol}.json").read_text(
                encoding="utf-8"
            )
        )["scenarios"]
    ]
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text.split("## 실제 구독 없이 두 구독을 가정한 테스트")[1].split("## 읽는 순서")[0]
    cells = [
        [cell.strip() for cell in line.split("|")[1:-1]]
        for line in section.splitlines()
        if line.startswith("|")
    ]
    for scenario in (
        "subscription_429",
        "subscription_503",
        "subscription_request_headroom",
        "subscription_token_headroom",
        "correlated_429",
    ):
        selected = [row for row in rows if row["scenario"] == scenario]
        observed = (
            f"{sum(row['successes'] for row in selected)}/"
            f"{sum(row['requests'] for row in selected)}"
        )
        assert next(row[2] for row in cells if len(row) == 4 and row[1] == scenario) == observed
    single_rows = [
        row
        for protocol in ("http", "sse")
        for row in json.loads(
            (ROOT / "docs" / "results" / f"resilience-{protocol}.json").read_text(encoding="utf-8")
        )["scenarios"]
    ]
    combined = rows + single_rows
    totals = [
        len(combined),
        sum(row["requests"] for row in combined),
        sum(row["successes"] for row in combined),
        sum(row["failures"] for row in combined),
    ]
    summary = next(line for line in text.splitlines() if line.startswith("| 합계 |"))
    assert [int(cell.strip().replace(",", "")) for cell in summary.split("|")[2:-1]] == totals
    manifest = json.loads((ROOT / "docs" / "verification.json").read_text(encoding="utf-8"))
    assert [
        manifest["combined_results"][name]
        for name in ("scenarios", "logical_requests", "successes", "expected_failures")
    ] == totals
