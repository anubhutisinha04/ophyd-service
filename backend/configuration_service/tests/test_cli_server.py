"""Server-mode CLI guards.

The service keeps lock state, PV health, and the device registry in
per-process memory, so more than one worker would diverge. ``main`` must
reject ``--workers > 1`` before it ever hands off to uvicorn.
"""

import pytest

from configuration_service import cli


def test_workers_gt_one_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["configuration-service", "--load-strategy", "mock", "--workers", "2"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    # argparse parser.error exits with code 2.
    assert exc.value.code == 2


def test_workers_one_is_accepted(monkeypatch):
    called = {}

    def _fake_run(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(
        "sys.argv",
        ["configuration-service", "--load-strategy", "mock", "--workers", "1"],
    )
    monkeypatch.setattr("uvicorn.run", _fake_run)
    cli.main()
    assert called.get("workers") == 1
