import subprocess
import sys
from pathlib import Path

SCRIPT = Path.home() / ".hermes" / "skills" / "software-development" / "delivery-verification" / "scripts" / "claim_audit.py"
FIXTURES = Path(__file__).parent / "fixtures"


def run(*args, stdin=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, input=stdin)


def test_help_exits_zero():
    result = run("--help")
    assert result.returncode == 0 and "audit" in result.stdout


def test_unanchored_fixture_is_flagged():
    result = run(str(FIXTURES / "claims_unanchored.txt"))
    assert result.returncode == 1
    assert "0 remaining" in result.stdout
    assert "verified" in result.stdout


def test_anchored_fixture_passes():
    result = run(str(FIXTURES / "claims_anchored.txt"))
    assert result.returncode == 0, result.stdout


def test_stdin_reads_dash():
    result = run("-", stdin="0 remaining unbound\n")
    assert result.returncode == 1 and "0 remaining" in result.stdout
