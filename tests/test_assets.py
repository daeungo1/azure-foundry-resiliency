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


def test_svg_arrows_have_single_paths_and_explicit_target_ports() -> None:
    def vertices(data: str) -> list[tuple[float, float]]:
        tokens = iter(re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", data))
        points = []
        x = y = 0.0
        for command in tokens:
            assert command in {"M", "H", "V"}, f"Review non-orthogonal connector: {data}"
            if command == "M":
                x, y = float(next(tokens)), float(next(tokens))
            elif command == "H":
                x = float(next(tokens))
            else:
                y = float(next(tokens))
            points.append((x, y))
        return points

    arrows = 0
    for path in (ROOT / "docs" / "diagrams").glob("*.svg"):
        root = ElementTree.parse(path).getroot()
        named = [element for element in root.iter() if element.get("id")]
        by_id = {element.attrib["id"]: element for element in named}
        assert len(by_id) == len(named), f"Duplicate SVG IDs: {path.name}"
        for element in root.iter("{http://www.w3.org/2000/svg}path"):
            if not set(element.get("class", "").split()) & {"line", "a"}:
                continue
            arrows += 1
            data = element.attrib["d"]
            assert len(re.findall("[Mm]", data)) == 1, (
                f"{path.name}: each arrow needs its own continuous path: {data}"
            )
            target_id = element.get("data-target")
            assert target_id is not None and target_id in by_id, (
                f"{path.name}: missing arrow target: {data}"
            )
            target = by_id[target_id]
            assert target.tag == "{http://www.w3.org/2000/svg}rect"
            x, y, width, height = (
                float(target.attrib[name]) for name in ("x", "y", "width", "height")
            )
            points = vertices(data)
            (before_x, before_y), (end_x, end_y) = points[-2:]
            if end_x > before_x and end_y == before_y:
                assert end_x == pytest.approx(x - 6)
                assert y + 8 <= end_y <= y + height - 8
            elif end_x < before_x and end_y == before_y:
                assert end_x == pytest.approx(x + width + 6)
                assert y + 8 <= end_y <= y + height - 8
            elif end_y > before_y and end_x == before_x:
                assert end_y == pytest.approx(y - 6)
                assert x + 12 <= end_x <= x + width - 12
            else:
                assert end_x == before_x and end_y < before_y
                assert end_y == pytest.approx(y + height + 6)
                assert x + 12 <= end_x <= x + width - 12
    assert arrows == 16


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
    single_rows = [
        row
        for protocol in ("http", "sse")
        for row in json.loads(
            (ROOT / "docs" / "results" / f"resilience-{protocol}.json").read_text(encoding="utf-8")
        )["scenarios"]
    ]
    two_rows = [
        row
        for protocol in ("http", "sse")
        for row in json.loads(
            (ROOT / "docs" / "results" / f"two-subscriptions-{protocol}.json").read_text(
                encoding="utf-8"
            )
        )["scenarios"]
    ]
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "reference implementation입니다." not in text
    assert text.index("## 권장 BP 아키텍처") < text.index("## 적용 효과와 참고 지표")
    assert text.index("## 적용 효과와 참고 지표") < text.index("## 검증 근거")
    effect_section = text.split("## 적용 효과와 참고 지표")[1].split("## 운영 적용")[0]
    assert "장애 주입" in effect_section and "운영 SLA" in effect_section
    table = {
        cells[0]: cells[1:]
        for line in text.splitlines()
        if line.startswith("|")
        if (cells := [cell.strip() for cell in line.split("|")[1:-1]])
    }
    manifest = json.loads((ROOT / "docs" / "verification.json").read_text(encoding="utf-8"))
    assert not manifest["bp_reference_metrics"]["live_customer_sla_measurement"]
    for label, scenario in (
        ("모델 전체 429", "same_model_429"),
        ("모델 전체 503", "same_model_503"),
        ("요청 예산 소진", "headroom"),
    ):
        baseline = [
            row
            for row in single_rows
            if row["scenario"] == scenario and row["routing_mode"] == "same"
        ]
        target_rows = two_rows if scenario == "headroom" else single_rows
        target_mode = "same" if scenario == "headroom" else "mixed"
        target = [
            row
            for row in target_rows
            if row["scenario"] == scenario and row["routing_mode"] == target_mode
        ]
        assert table[label][:2] == [
            f"{sum(row['successes'] for row in selected)}/"
            f"{sum(row['requests'] for row in selected)}"
            for selected in (baseline, target)
        ]
        recorded = manifest["bp_reference_metrics"]["comparisons"][scenario]
        assert recorded["control_successes"] == sum(row["successes"] for row in baseline)
        assert recorded["bp_successes"] == sum(row["successes"] for row in target)
        assert recorded["requests_per_configuration"] == sum(row["requests"] for row in target)
    rows = [
        row
        for row in single_rows
        if row["scenario"] == "same_model_429" and row["routing_mode"] == "mixed"
    ]
    assert all(
        [len(sample["attempts"]) for sample in row["samples"]] == [5] + [1] * 9 for row in rows
    )
    assert "5회" in effect_section and "1회" in effect_section


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
    conditions = {
        "subscription_429",
        "subscription_503",
        "subscription_request_headroom",
        "subscription_token_headroom",
    }
    selected = [row for row in rows if row["scenario"] in conditions]
    observed = (
        f"{sum(row['successes'] for row in selected)}/{sum(row['requests'] for row in selected)}"
    )
    assert f"**{observed}**" in text
    assert sum(row["degraded_responses"] for row in selected) == 0
    assert "--subscriptions 2 --regions 4" in text
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
