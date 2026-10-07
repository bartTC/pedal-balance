"""intervals.icu API client, credentials and the download cache."""
import base64
import datetime
import gzip
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

KEYS = ("INTERVALS_ICU_API_KEY", "INTERVALS_ICU_ATHLETE_ID")
# Carried over from the old activity when replacing it. Load, NP etc. are
# recomputed by intervals.icu; a manually set load is not carried over on purpose.
CARRY = ("type", "sub_type", "commute", "trainer", "race", "icu_rpe", "feel", "tags")
TIMEOUT_S = 120

Activity = dict[str, Any]   # activity as returned by the API


class ConfigError(Exception):
    """Missing or wrong input: credentials, files, targets."""


class ReplaceError(Exception):
    """Replacing an activity stopped; the message says what is left to do by hand."""


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pedal-balance"


def cache_dir() -> Path:
    if os.environ.get("PEDAL_BALANCE_CACHE"):
        return Path(os.environ["PEDAL_BALANCE_CACHE"])
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pedal-balance"


def read_env_file(path: Path) -> dict[str, str]:
    env = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    return env


def credentials() -> tuple[str, str]:
    """
    API key and athlete ID.

    Environment first, then .env in the current folder, then ~/.config/pedal-balance/.env.
    """
    found = {}
    for path in (config_dir() / ".env", Path(".env")):
        if path.is_file():
            found.update({k: v for k, v in read_env_file(path).items() if k in KEYS})
    found.update({k: os.environ[k] for k in KEYS if os.environ.get(k)})
    missing = [k for k in KEYS if not found.get(k)]
    if missing:
        msg = (f"Missing {', '.join(missing)}. Set them in the environment, in .env in the current "
               f"folder or in {config_dir() / '.env'}.")
        raise ConfigError(msg)
    return found["INTERVALS_ICU_API_KEY"], found["INTERVALS_ICU_ATHLETE_ID"]


class Intervals:
    def __init__(self, key: str, athlete: str) -> None:
        self.athlete = athlete
        self.headers = {
            "Authorization": "Basic " + base64.b64encode(f"API_KEY:{key}".encode()).decode(),
            # With urllib's default User-Agent Cloudflare answers 403.
            "User-Agent": "pedal-balance",
        }

    def request(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None,
                **params: str | int) -> tuple[int, bytes]:
        """(HTTP status, response body) of an API call."""
        query = "?" + urllib.parse.urlencode(params) if params else ""
        headers = dict(self.headers, **({"Content-Type": content_type} if content_type else {}))
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"https://intervals.icu/api/v1{path}{query}", body, headers,
                                       method=method),
                timeout=TIMEOUT_S,
            ) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                msg = f"intervals.icu: {e.code} {e.reason} – check the API key or athlete ID."
                raise ConfigError(msg) from e
            raise

    def get(self, path: str, **params: str | int) -> bytes:
        return self.request("GET", path, **params)[1]

    def activity(self, act_id: str) -> Activity:
        return json.loads(self.get(f"/activity/{act_id}"))

    def rides(self, oldest: datetime.date, newest: datetime.date) -> list[Activity]:
        acts = json.loads(self.get(f"/athlete/{self.athlete}/activities",
                                   oldest=oldest.isoformat(), newest=newest.isoformat()))
        return sorted((a for a in acts if "Ride" in (a.get("type") or "")),
                      key=lambda a: a.get("start_date_local") or "")

    def gear_names(self) -> dict[str, str]:
        try:
            return {g["id"]: g.get("name") for g in json.loads(self.get(f"/athlete/{self.athlete}/gear"))}
        except (urllib.error.URLError, ValueError):
            return {}

    def download(self, act_id: str, folder: Path) -> Path:
        """Path of the original file, downloaded unless it is already cached."""
        path = Path(folder) / f"{act_id}.fit"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self.get(f"/activity/{act_id}/file")
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        return path

    def upload(self, path: Path, **params: str | int | None) -> list[str]:
        """
        Upload a FIT as a new activity. Returns the IDs of the newly created activities.

        intervals.icu detects duplicates by the hash of the file and then creates nothing.
        """
        sep = uuid.uuid4().hex
        body = (f'--{sep}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{path.name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() \
            + f"\r\n--{sep}--\r\n".encode()
        status, resp = self.request("POST", f"/athlete/{self.athlete}/activities", body,
                                    f"multipart/form-data; boundary={sep}",
                                    **{k: v for k, v in params.items() if v is not None})
        if status != HTTPStatus.CREATED:
            return []
        return [a["id"] for a in json.loads(resp).get("activities") or []]

    def update(self, act_id: str, fields: dict[str, Any]) -> None:
        self.request("PUT", f"/activity/{act_id}", json.dumps(fields).encode(), "application/json")

    def delete(self, act_id: str) -> None:
        self.request("DELETE", f"/activity/{act_id}")


class Api(Protocol):
    """What replace_activity needs from the client (tests pass a fake)."""

    def upload(self, path: Path, **params: str | int | None) -> list[str]: ...
    def update(self, act_id: str, fields: dict[str, Any]) -> None: ...
    def delete(self, act_id: str) -> None: ...
    def activity(self, act_id: str) -> Activity: ...


def activity_url(act_id: str) -> str:
    return f"https://intervals.icu/activities/{act_id}"


def replace_activity(api: Api, old: Activity, path: Path, say: Callable[[str], object] = print) -> str:
    """
    Upload the corrected file, carry over the details, then delete the old activity.

    Uploading comes first: if intervals.icu creates nothing (duplicate hash) or
    carrying over the details fails, the old activity stays and ReplaceError is
    raised. Returns the ID of the new activity.
    """
    new_ids = api.upload(path, name=old.get("name"), description=old.get("description"),
                         external_id=Path(path).name, paired_event_id=old.get("paired_event_id"))
    if not new_ids:
        msg = "intervals.icu created nothing (file already there?). The old activity stays."
        raise ReplaceError(msg)
    if len(new_ids) > 1:
        msg = f"Several activities created ({', '.join(new_ids)}), the old one stays. Please check by hand."
        raise ReplaceError(msg)
    new_id = new_ids[0]
    new_url = activity_url(new_id)
    say(f"Uploaded: {new_url}")

    fields = {k: old[k] for k in CARRY if old.get(k) is not None}
    if (old.get("gear") or {}).get("id"):
        fields["gear"] = {"id": old["gear"]["id"]}
    try:
        api.update(new_id, fields)
    except urllib.error.URLError as e:
        msg = f"Carrying over the details failed ({e}). The old activity stays, new one: {new_url}"
        raise ReplaceError(msg) from e
    say(f"Carried over: {', '.join(fields) or 'nothing'}")

    old_url = activity_url(old["id"])
    try:
        api.delete(old["id"])
    except urllib.error.URLError as e:
        msg = f"Deleting failed ({e}). Please delete by hand: {old_url} (new: {new_url})"
        raise ReplaceError(msg) from e
    say(f"Deleted: {old_url}")

    # A workout can only be paired with one activity; while the old one existed,
    # the pairing during the upload may not have taken effect.
    if old.get("paired_event_id"):
        try:
            if not api.activity(new_id).get("paired_event_id"):
                api.update(new_id, {"paired_event_id": old["paired_event_id"]})
                say("Workout paired again")
        except urllib.error.URLError as e:
            say(f"Workout not paired ({e}), please do it by hand.")
    return new_id
