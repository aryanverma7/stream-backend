import asyncio

import pytest

from stream import widget_hub as wh


class FakeWS:
    def __init__(self, behaviour=None):
        self.sent, self.behaviour, self.closed = [], behaviour, False

    async def send_json(self, m):
        if self.behaviour == "raise":
            raise RuntimeError("closing transport")
        if self.behaviour == "hang":
            await asyncio.sleep(60)
        self.sent.append(m)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_a_failing_or_stalled_client_is_dropped_and_the_rest_still_get_it(monkeypatch):
    monkeypatch.setattr(wh, "SEND_TIMEOUT_SECONDS", 0.05)
    hub = wh.WidgetHub()
    good, bad, slow, other = FakeWS(), FakeWS("raise"), FakeWS("hang"), FakeWS()
    hub._clients = {"g": (good, "roulette"), "b": (bad, "roulette"), "s": (slow, "roulette"), "o": (other, "chat")}
    await hub.broadcast({"x": 1}, tag="roulette")
    assert good.sent == [{"x": 1}] and other.sent == []
    assert set(hub._clients) == {"g", "o"}


@pytest.mark.asyncio
async def test_a_client_leaving_mid_broadcast_does_not_break_it():
    hub = wh.WidgetHub()

    class Leaver(FakeWS):
        async def send_json(self, m):
            hub._clients.pop("l", None)  # what handle_connection's finally does
            await asyncio.sleep(0)

    keep = FakeWS()
    hub._clients = {"l": (Leaver(), "t"), "k": (keep, "t")}
    await hub.broadcast({"y": 2})
    assert keep.sent == [{"y": 2}]
