import json

import pytest
from conftest import PYTEST_CMD

from patchproof import cli
from patchproof.pipeline import Report
from patchproof.report import render_text
from patchproof.verdict import Verdict

BUGGY = "def last(xs):\n    return xs[len(xs)]\n"
FIXED = "def last(xs):\n    return xs[-1] if xs else None\n"
TEST = "from calc import last\n\n\ndef test_last():\n    assert last([4, 5]) == 5\n    assert last([]) is None\n"


@pytest.fixture
def files(scenario, tmp_path):
    repo, patch = scenario({"calc.py": BUGGY}, {"calc.py": FIXED, "tests/test_calc.py": TEST})
    pf = tmp_path / "fix.patch"
    pf.write_text(patch)
    return repo, pf


def test_json_output_contract_and_exit_zero(files, capsys):
    repo, pf = files
    code = cli.main(["check", "--repo", str(repo), "--patch", str(pf), "--test-cmd", PYTEST_CMD,
                     "--runs", "1", "--json"])  # fmt: skip
    out = capsys.readouterr().out
    data = json.loads(out)  # exactly one JSON object on stdout
    assert code == 0
    assert set(data) == {"verdict", "summary", "evidence", "hunks", "timings"}
    assert data["verdict"] == "PROVEN"


def test_exit_code_one_for_non_proven(files, capsys):
    repo, pf = files
    code = cli.main(["check", "--repo", str(repo), "--patch", str(pf), "--test-cmd", "true",
                     "--runs", "1", "--json", "--no-mutation"])  # fmt: skip
    assert json.loads(capsys.readouterr().out)["verdict"] == "NO_OP"
    assert code == 1


def test_exit_code_two_for_error_and_json_still_valid(tmp_path, capsys):
    code = cli.main(["check", "--repo", str(tmp_path), "--patch", str(tmp_path / "missing"),
                     "--test-cmd", "true", "--json"])  # fmt: skip
    assert code == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "ERROR"


def test_missing_test_cmd_is_usage_error(files, capsys):
    repo, pf = files
    assert cli.main(["check", "--repo", str(repo), "--patch", str(pf)]) == 2


def test_missing_required_args_exit_two():
    with pytest.raises(SystemExit) as e:
        cli.main(["check"])
    assert e.value.code == 2


def test_text_format_card(files, capsys):
    repo, pf = files
    code = cli.main(["check", "--repo", str(repo), "--patch", str(pf), "--test-cmd", PYTEST_CMD,
                     "--runs", "1", "--no-mutation"])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0 and "PROVEN" in out and "calc.py#1" in out


def test_render_text_each_verdict():
    for v in Verdict:
        r = Report(v, f"summary for {v}", {"base_runs": [{"passed": False}]}, [], {"total": 1.0})
        text = render_text(r)
        assert str(v) in text and f"summary for {v}" in text
