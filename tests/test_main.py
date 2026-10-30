import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import main


@pytest.mark.asyncio
async def test_shutdown_stops_every_background_task_main_starts(monkeypatch):
    stopped = []

    def rec(name):
        async def stop():
            stopped.append(name)
        return stop

    for mod, attr in ((main.discord_live, "stop_poller"), (main.break_timer, "stop_broadcaster"),
                      (main.spotify, "stop_now_playing_poller"), (main.scheduler, "stop"),
                      (main.health_checks, "stop"), (main.streamerbot, "stop")):
        monkeypatch.setattr(mod, attr, rec(f"{getattr(mod, '__name__', 'streamerbot')}.{attr}"))
    monkeypatch.setattr(main, "stop_tips_listener", rec("tips"))

    class Runner:
        async def cleanup(self):
            stopped.append("runner")

    await main.shutdown(Runner())
    assert "stream.discord_live.stop_poller" in stopped
    assert len(stopped) == 8 and stopped[-1] == "runner"
