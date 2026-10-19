"""CLI exit codes and output files are part of the guide's runnable contract."""

import json
from pathlib import Path

from foundry_resiliency.cli import main, percentile


def test_example_and_validate_cli(tmp_path: Path) -> None:
    path = tmp_path / "same.json"
    assert main(["example", "--mode", "same", "--output", str(path)]) == 0
    report = tmp_path / "validation.json"
    assert main(["validate-config", "--config", str(path), "--output", str(report)]) == 0
    assert json.loads(report.read_text())["backends"] == 16


def test_live_calls_require_explicit_confirmation(tmp_path: Path) -> None:
    path = tmp_path / "same.json"
    assert main(["example", "--mode", "same", "--output", str(path)]) == 0
    assert main(["smoke", "--config", str(path), "--model", "gpt-6.1-sol"]) == 2


def test_nearest_rank_percentile() -> None:
    assert percentile([3, 1, 2], 0.95) == 3


def test_cli_accepts_region_aliases_and_neutral_expansion_flag(tmp_path: Path) -> None:
    from foundry_resiliency.cli import create_parser

    args = create_parser().parse_args(
        [
            "smoke",
            "--config",
            "test.local.json",
            "--model",
            "gpt-6.1-sol",
            "--market",
            "Region02",
        ]
    )
    assert args.market == "Region02"
    path = tmp_path / "expanded.json"
    assert main(["example", "--mode", "mixed", "--include-expansion", "--output", str(path)]) == 0
    assert len(json.loads(path.read_text())["backends"]) == 20
