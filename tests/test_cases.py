"""
Regression tests with the two real cases (README: case 1 and case 2).

The originals definitely contain an error: the pedals were re-zeroed after 60 and
15 min. case2_corrected.fit was written by the current correction and uploaded
to intervals.icu; case1_corrected.fit comes from an older version (balance in the
section set to 48 instead of invalid). All fixtures are anonymized (tests/anonymize.py).
"""
import argparse

import pytest

from pedal_balance import cli
from pedal_balance.correct import Options, correct, verify
from pedal_balance.detect import check, is_corrected


@pytest.mark.parametrize(("name", "span", "sec", "rest"), [
    ("case1.fit", "Start–59:37", 41.3, 47.5),   # reset after 60 min
    ("case2.fit", "Start–15:14", 39.3, 50.0),   # reset after 15 min
])
def test_step_at_start(load_fit, name, span, sec, rest):
    f = check(load_fit(name))
    assert f.step is not None
    assert f.step.span == span
    assert f.step.sec == pytest.approx(sec, abs=0.1)
    assert f.step.rest == pytest.approx(rest, abs=0.1)
    assert f.step.at_start
    assert f.bias is None   # the rest is close to 50/50


@pytest.mark.parametrize("name", ["case1_corrected.fit", "case2_corrected.fit"])
def test_corrected_files_are_ok(load_fit, name):
    f = check(load_fit(name))
    assert f.step is None
    assert f.bias is None


def test_corrected_suffixes():
    assert is_corrected("i123_corrected.fit")
    assert is_corrected("i123_korrigiert.fit")
    assert not is_corrected("i123.fit")
    assert not is_corrected(None)


def test_case2_correction_unchanged(load_fit, fixtures):
    """Correcting case 2 gives byte for byte the uploaded file."""
    fit = load_fit("case2.fit")
    orig = bytes(fit.buf)
    c = correct(fit, Options(until=900))
    assert c.ok
    assert c.snapped
    assert bytes(fit.buf) == (fixtures / "case2_corrected.fit").read_bytes()
    verify(orig, fit)


def test_case1_fails_the_speed_cross_check(load_fit):
    c = correct(load_fit("case1.fit"), Options(until=3577))
    assert not c.ok
    assert [ok for _, ok, _ in c.checks] == [True, True, False]


def test_parse_target(fixtures):
    url = "https://intervals.icu/activities/i123456789"
    assert cli.parse_target(url) == ("i123456789", None)
    assert cli.parse_target(url + "/power") == ("i123456789", None)
    assert cli.parse_target("i123456789") == ("i123456789", None)
    assert cli.parse_target(str(fixtures / "case2.fit")) == (None, fixtures / "case2.fit")


def test_boundary_from_step(load_fit):
    args = argparse.Namespace(until=None, no_snap=False)
    assert cli.find_until(load_fit("case2.fit"), args) == (914, True)
    assert cli.find_until(load_fit("case1.fit"), args) == (3577, True)


def test_fix_local_file(fixtures, tmp_path, monkeypatch):
    out = tmp_path / "out.fit"
    argv = ["fix", str(fixtures / "case2.fit"), "-o", str(out)]
    monkeypatch.setattr(cli, "ask", lambda _: False)
    assert cli.main(argv) == 0
    assert not out.exists()
    monkeypatch.setattr(cli, "ask", lambda _: True)
    assert cli.main(argv) == 0
    assert out.read_bytes() == (fixtures / "case2_corrected.fit").read_bytes()
