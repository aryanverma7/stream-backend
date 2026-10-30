import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parent.parent / "deploy" / "com.dualbladex.backend.plist"


def test_plist_drops_stdout_keeps_stderr_and_runs_the_venv():
    p = plistlib.loads(PLIST.read_bytes())
    assert p["StandardOutPath"] == "/dev/null"  # logs/backend.log already has every line
    assert p["StandardErrorPath"] == "/tmp/mac_mini_backend.err.log"  # crash tracebacks
    home = "/Users/YOUR_MAC_USER/Documents/Stream"
    assert p["ProgramArguments"] == [f"{home}/venv/bin/python3", f"{home}/main.py"]
    assert p["WorkingDirectory"] == home
