"""replace_activity against a fake intervals.icu client: upload first, delete last."""
import urllib.error
from pathlib import Path

import pytest

from pedal_balance.intervals import ReplaceError, replace_activity

OLD = {"id": "i100", "name": "Morning Ride", "description": "notes", "type": "GravelRide",
       "icu_rpe": 5, "feel": 2, "tags": ["race"], "commute": None, "gear": {"id": "b42"},
       "paired_event_id": 7}
PATH = Path("x/i100_corrected.fit")


class FakeApi:
    def __init__(self, upload=("i200",), fail_update=False, paired=None):
        self.calls, self._upload, self._fail_update, self._paired = [], list(upload), fail_update, paired

    def upload(self, path, **params):
        self.calls.append(("upload", path, params))
        return self._upload

    def update(self, act_id, fields):
        self.calls.append(("update", act_id, fields))
        if self._fail_update:
            msg = "boom"
            raise urllib.error.URLError(msg)

    def delete(self, act_id):
        self.calls.append(("delete", act_id))

    def activity(self, act_id):
        return {"id": act_id, "paired_event_id": self._paired}


def kinds(api):
    return [c[0] for c in api.calls]


def test_success():
    api = FakeApi()
    assert replace_activity(api, OLD, PATH, say=lambda _: None) == "i200"
    assert kinds(api) == ["upload", "update", "delete", "update"]
    assert api.calls[0] == ("upload", PATH, {"name": "Morning Ride", "description": "notes",
                                             "external_id": "i100_corrected.fit", "paired_event_id": 7})
    assert api.calls[1] == ("update", "i200", {"type": "GravelRide", "icu_rpe": 5, "feel": 2,
                                               "tags": ["race"], "gear": {"id": "b42"}})
    assert api.calls[2] == ("delete", "i100")
    assert api.calls[3] == ("update", "i200", {"paired_event_id": 7})


def test_no_repair_when_already_paired():
    api = FakeApi(paired=7)
    replace_activity(api, OLD, PATH, say=lambda _: None)
    assert kinds(api) == ["upload", "update", "delete"]


def test_duplicate_keeps_old_activity():
    api = FakeApi(upload=())
    with pytest.raises(ReplaceError, match="created nothing"):
        replace_activity(api, OLD, PATH, say=lambda _: None)
    assert "delete" not in kinds(api)


def test_failed_update_keeps_old_activity():
    api = FakeApi(fail_update=True)
    with pytest.raises(ReplaceError, match="old activity stays"):
        replace_activity(api, OLD, PATH, say=lambda _: None)
    assert "delete" not in kinds(api)
