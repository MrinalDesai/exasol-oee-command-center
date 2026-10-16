"""The pillar-evidence script: query mix is valid, and the concurrency harness counts correctly."""
from contextlib import contextmanager

from oee.pillars import QUERY_MIX, _concurrency
from oee.preflight import CHECKS, _same


def test_query_mix_runs(duck):
    for q in QUERY_MIX:
        duck.query(q)


def test_concurrency_harness(duck):
    class Conn:
        def execute(self, q):
            rows = duck.query(q)
            return type("R", (), {"fetchall": lambda self: rows})()

    @contextmanager
    def connect():
        yield Conn()

    r = _concurrency(3, 1.0, connect)
    assert r["queries"] > 0 and r["qps"] > 0 and not r["errors"]
    assert r["p95_ms"] >= r["p50_ms"]


def test_preflight_comparisons():
    assert _same(2.0, 2) and _same("2RS", "2RS") and not _same("ABC", "AB")
    assert len({c[0] for c in CHECKS}) == len(CHECKS)
