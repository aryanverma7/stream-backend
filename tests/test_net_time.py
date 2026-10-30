import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import net_time


class _Resp:
    def __init__(self, date):
        self.headers = {} if date is None else {"Date": date}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """answers[url] = (head Date or Exception, get Date)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _req(self, method, url):
        self.calls.append((method, url))
        v = self.answers[url][0 if method == "head" else 1]
        if isinstance(v, Exception):
            raise v
        return _Resp(v)

    def head(self, url, **kw):
        return self._req("head", url)

    def get(self, url, **kw):
        return self._req("get", url)


G, C = net_time.HOSTS
D1 = "Wed, 28 Oct 2026 12:00:00 GMT"
D2 = "Wed, 28 Oct 2026 12:00:10 GMT"
T1 = 1793188800.0


def test_parse_date():
    assert net_time.parse_date(D1) == T1
    assert net_time.parse_date("not a date") is None
    assert net_time.parse_date("") is None
    assert net_time.parse_date(None) is None


@pytest.mark.asyncio
async def test_median_of_two_answers():
    s = _Session({G: (D1, None), C: (D2, None)})
    assert await net_time.online_time(lambda: s) == T1 + 5


@pytest.mark.asyncio
async def test_falls_back_to_get_and_skips_malformed():
    s = _Session({G: (OSError("refused"), D1), C: ("garbage", "garbage")})
    assert await net_time.online_time(lambda: s) == T1
    assert ("get", G) in s.calls


@pytest.mark.asyncio
async def test_no_answer_is_none():
    s = _Session({G: (OSError(), OSError()), C: (None, None)})
    assert await net_time.online_time(lambda: s) is None
