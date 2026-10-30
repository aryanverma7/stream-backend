import logging.handlers
from pathlib import Path

from core import logger
from core.dashboard_api import tail_lines


def test_the_suite_never_writes_the_real_log():
    real = Path(logger.__file__).parent.parent / "logs"
    assert logger.LOG_DIR.resolve() != real.resolve()


def test_one_rotating_file_handler_is_shared():
    a, b = logger.get_logger("AuditA"), logger.get_logger("AuditB")
    fa = [h for h in a.handlers if isinstance(h, logging.FileHandler)]
    assert len(fa) == 1 and isinstance(fa[0], logging.handlers.RotatingFileHandler)
    assert fa[0].maxBytes > 0 and fa[0] in b.handlers


def test_tail_reads_only_the_end(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line {i}\n" for i in range(1000)))
    assert tail_lines(p, 3, block=7) == ["line 997", "line 998", "line 999"]
    assert tail_lines(p, 5000) == [f"line {i}" for i in range(1000)]
    assert tail_lines(p, 0) == []
