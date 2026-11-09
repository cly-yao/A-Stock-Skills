import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_every_python_file_parses():
    failures = []
    for path in _python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{path}:{exc.lineno}: {exc.msg}")
    assert failures == []


def test_hyphenated_imports_are_gone_from_entrypoints():
    screener = (ROOT / "skills/05-quant/screener/main.py").read_text(encoding="utf-8")
    daily = (ROOT / "daily_update.py").read_text(encoding="utf-8")
    assert "from skills.01-infra" not in screener
    assert "from skills.04-stock-analysis" not in screener
    assert "from skills.01-infra" not in daily
    report = (ROOT / "skills/05-reports/report/main.py").read_text(encoding="utf-8")
    assert "stock_zh_a_spot_em" not in report
    assert "stock_zh_index_spot_em" not in report
    watch = (ROOT / "skills/02-data-collection/watchlist-monitor/main.py").read_text(encoding="utf-8")
    assert "stock_zh_a_spot_em" not in watch


def test_cli_help_and_refused_full_market_update():
    commands = [
        [sys.executable, "skills/05-quant/screener/main.py", "list"],
        [sys.executable, "skills/06-tools/alerter/main.py", "--help"],
        [sys.executable, "skills/05-reports/report/main.py", "--help"],
        [sys.executable, "skills/01-infra/astock-data-source/main.py", "--help"],
    ]
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr
    refused = subprocess.run(
        [sys.executable, "daily_update.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert refused.returncode == 2
    assert "UNBOUNDED_REFUSED" in refused.stdout
