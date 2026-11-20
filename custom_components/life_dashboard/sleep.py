"""Sleep stages: what a night was made of, per session, read by its wake date.

The running sum `sleep_minutes` books every session whole on the day it ended, naps and a
second app included. The stage statistics answer another question: how the night went. So
they are read from one session per wake date, the main one, and a late or backfilled
night only ever touches the row of its own date.

A phone's stages do not add up by themselves. An iPhone night holds overlapping samples
from the phone and the watch, and an `in_bed` sample that spans the whole night, so stages
are filtered on the session's source and then swept into one timeline that counts every
moment once. Android sends the stages of one record, which may be none at all.

The ledger keeps what is in `Ledger.sleep`:

nights: identity -> day, start, end, source, the moment of the payload (at) and minutes per
stage, under the same retention as the sessions.
stages: identity -> the samples, for the sessions of the last KEPT_NIGHTS wake dates, so a
deleted stage can be taken out of its night, and a deleted night falls back to the one
before it with everything the sensors show.
gone: identity -> at of the deletion, so an older payload cannot bring a night back.
kept_from: the first date taken in, once anything was pruned.

Nothing here imports from Home Assistant.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final, NamedTuple

from .payload import (
    KEY_LAST_SLEEP_ASLEEP,
    KEY_LAST_SLEEP_AWAKE,
    KEY_LAST_SLEEP_DEEP,
    KEY_LAST_SLEEP_LIGHT,
    KEY_LAST_SLEEP_REM,
    SensorUpdate,
    is_own_record,
    parse_instant,
    tile_hours,
)

#: The running sum's key in the ledger's sessions.
SLEEP_SESSIONS: Final = "sleep_minutes"

#: How many wake dates keep their samples.
KEPT_NIGHTS: Final = 14

#: Statistic key -> the name of its minutes in a night row. In hours as a statistic.
NIGHT_STATISTICS: Final[dict[str, str]] = {
    "sleep_deep": "deep",
    "sleep_light": "light",
    "sleep_rem": "rem",
    "sleep_awake": "awake",
    "sleep_asleep": "asleep",
}

# What the phones send, and the name it goes by here. These are the names of the `stages`
# attribute, except in_bed, which is never in it. awake_in_bed is plain awake.
_STAGE_NAMES: Final[dict[str, str]] = {
    "awake": "awake",
    "awake_in_bed": "awake",
    "out_of_bed": "out_of_bed",
    "light": "light",
    "deep": "deep",
    "rem": "rem",
    "sleeping": "sleeping",
    "unknown": "unknown",
    "in_bed": "in_bed",
}

# Who wins a moment two samples claim: the most specific reading first.
_PRIORITY: Final[dict[str, int]] = {
    "deep": 0,
    "rem": 1,
    "light": 2,
    "sleeping": 3,
    "awake": 4,
    "out_of_bed": 5,
    "unknown": 6,
}

_ASLEEP: Final = ("deep", "light", "rem", "sleeping")
_BREAKDOWN: Final = ("deep", "light", "rem")

# Sensor key -> the name of its minutes in a night row.
_LAST_NIGHT_KEYS: Final[dict[str, str]] = {
    KEY_LAST_SLEEP_DEEP: "deep",
    KEY_LAST_SLEEP_LIGHT: "light",
    KEY_LAST_SLEEP_REM: "rem",
    KEY_LAST_SLEEP_AWAKE: "awake",
}

LAST_NIGHT_KEYS: Final = (KEY_LAST_SLEEP_ASLEEP, *_LAST_NIGHT_KEYS)


class Sample(NamedTuple):
    """One stage as a phone sent it, in seconds since the epoch."""

    stage: str
    start: float
    end: float
    uuid: str | None = None
    source: str | None = None


class Segment(NamedTuple):
    """A stretch of the night's one timeline."""

    stage: str
    start: float
    end: float


@dataclass(frozen=True)
class Night:
    """The newest main session, as the sensors read it."""

    day: str
    start: datetime
    end: datetime
    source: str | None
    minutes: dict[str, float]
    segments: list[Segment]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _instant(value: Any) -> datetime | None:
    try:
        return parse_instant(value)
    except ValueError:
        return None


def _stage_name(value: Any) -> str:
    """The name of a stage. Anything else that arrives is unknown, so no text of a
    payload ever becomes a stage name; the app's old enum names read as the new ones."""
    if not isinstance(value, str):
        return "unknown"
    return _STAGE_NAMES.get(value.strip().lower().removeprefix("stage_type_"), "unknown")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_samples(raw: Any) -> list[Sample]:
    """The readable stages of a session, oldest first.

    The times decide, in or out of the duration the phone sent, which only fills in a
    missing end or start. A stage that does not end after it starts is skipped.
    """
    if not isinstance(raw, list):
        return []
    samples: list[Sample] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        start = _instant(entry.get("start_time"))
        end = _instant(entry.get("end_time"))
        seconds = _finite(entry.get("duration_seconds"))
        if end is None and start is not None and seconds is not None and seconds > 0:
            end = start + timedelta(seconds=seconds)
        elif start is None and end is not None and seconds is not None and seconds > 0:
            start = end - timedelta(seconds=seconds)
        if start is None or end is None or end <= start:
            continue
        samples.append(
            Sample(
                _stage_name(entry.get("stage")),
                round(start.timestamp(), 3),
                round(end.timestamp(), 3),
                _text(entry.get("uuid")),
                _text(entry.get("source")),
            )
        )
    return sorted(samples, key=lambda sample: (sample.start, sample.end, sample.stage))


def timeline(samples: Iterable[Sample], source: str | None) -> list[Segment]:
    """One timeline for a session, in which no moment counts twice.

    Only the session's own source counts; a stage without a source is the session's. A
    session without a source (iOS before 1.5.0) keeps every stage. in_bed is no stage of
    sleep, and where stages overlap the most specific wins. Sorted, not overlapping, and
    touching segments of one stage are merged.
    """
    counted = sorted(
        (
            sample
            for sample in samples
            if sample.stage in _PRIORITY and (source is None or (sample.source or source) == source)
        ),
        key=lambda sample: sample.start,
    )
    edges = sorted({sample.start for sample in counted} | {sample.end for sample in counted})
    segments: list[Segment] = []
    active: list[tuple[int, float, str]] = []
    position = 0
    for left, right in zip(edges, edges[1:], strict=False):
        while position < len(counted) and counted[position].start <= left:
            sample = counted[position]
            heapq.heappush(active, (_PRIORITY[sample.stage], sample.end, sample.stage))
            position += 1
        while active and active[0][1] <= left:
            heapq.heappop(active)
        if not active:
            continue
        stage = active[0][2]
        if segments and segments[-1].stage == stage and segments[-1].end == left:
            segments[-1] = Segment(stage, segments[-1].start, right)
        else:
            segments.append(Segment(stage, left, right))
    return segments


def summarize(segments: Iterable[Segment]) -> dict[str, float]:
    """Minutes per stage of a timeline, with only what the phone can say.

    Nothing without time asleep. A night with a breakdown has all four, also at zero; a
    night with only `sleeping` knows how long it slept and no more, so it has no deep,
    light or REM.
    """
    minutes: dict[str, float] = {}
    for segment in segments:
        minutes[segment.stage] = minutes.get(segment.stage, 0.0) + (segment.end - segment.start)
    minutes = {stage: seconds / 60 for stage, seconds in minutes.items()}
    asleep = sum(minutes.get(stage, 0.0) for stage in _ASLEEP)
    if asleep <= 0:
        return {}
    awake = minutes.get("awake", 0.0) + minutes.get("out_of_bed", 0.0)
    summary = {"asleep": round(asleep, 2)}
    if any(stage in minutes for stage in _BREAKDOWN):
        summary |= {stage: round(minutes.get(stage, 0.0), 2) for stage in _BREAKDOWN}
        summary["awake"] = round(awake, 2)
    elif awake > 0:
        summary["awake"] = round(awake, 2)
    return summary


def payload_moment(data: dict[str, Any]) -> float | None:
    """When the payload was made, the order that says which of two versions is newer."""
    moment = _instant(data.get("timestamp"))
    return None if moment is None else moment.timestamp()


def session_identity(uuid: Any, moment: datetime, raw: float | None) -> str:
    """What a session is stored under. A record without a uuid still cannot be counted
    twice: its moment and value identify it well enough for the only case that matters,
    the same batch arriving again."""
    return uuid if isinstance(uuid, str) and uuid else f"{moment.isoformat()}:{raw}"


def _sample_row(sample: Sample) -> list[Any]:
    row: list[Any] = [sample.stage, sample.start, sample.end]
    if sample.source:
        return [*row, sample.uuid, sample.source]
    return [*row, sample.uuid] if sample.uuid else row


def _sample_rows(samples: Iterable[Sample]) -> list[list[Any]]:
    return [_sample_row(sample) for sample in samples]


def _samples(rows: Iterable[list[Any]]) -> list[Sample]:
    return [Sample(*row) for row in rows]


def clean_store(raw: Any) -> dict[str, Any]:
    """The kept nights as a ledger holds them; what cannot be read is left out."""
    if not isinstance(raw, dict):
        return {}
    nights = raw.get("nights")
    stages = raw.get("stages")
    gone = raw.get("gone")
    store: dict[str, Any] = {
        "nights": {
            key: dict(row)
            for key, row in (nights.items() if isinstance(nights, dict) else ())
            if _readable_night(row)
        },
        "stages": {
            key: [list(row) for row in rows]
            for key, rows in (stages.items() if isinstance(stages, dict) else ())
            if isinstance(rows, list) and all(_readable_sample(row) for row in rows)
        },
        "gone": {
            key: float(value)
            for key, value in (gone.items() if isinstance(gone, dict) else ())
            if _finite(value) is not None
        },
    }
    if isinstance(kept_from := raw.get("kept_from"), str):
        store["kept_from"] = kept_from
    return store


def _readable_night(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("day"), str)
        and _finite(row.get("start")) is not None
        and _finite(row.get("end")) is not None
    )


def _readable_sample(row: Any) -> bool:
    return (
        isinstance(row, list)
        and 3 <= len(row) <= 5
        and row[0] in _STAGE_NAMES
        and _finite(row[1]) is not None
        and _finite(row[2]) is not None
    )


def main_session(store: dict[str, Any], day: str) -> tuple[str, dict[str, Any]] | None:
    """The session that speaks for a wake date: most time asleep, then the longest span,
    then the uuid, so the choice does not depend on the order of arrival."""
    candidates = [
        (identity, row)
        for identity, row in store.get("nights", {}).items()
        if row["day"] == day and row.get("asleep")
    ]
    return max(
        candidates,
        key=lambda item: (item[1]["asleep"], item[1]["end"] - item[1]["start"], item[0]),
        default=None,
    )


def night_values(store: dict[str, Any], day: str) -> dict[str, float]:
    """A wake date's statistics in hours, from its main session; those it lacks left out."""
    main = main_session(store, day)
    if main is None:
        return {}
    row = main[1]
    return {key: row[name] / 60 for key, name in NIGHT_STATISTICS.items() if name in row}


@dataclass(frozen=True)
class _Incoming:
    """One readable sleep record of a payload."""

    identity: str
    day: str
    row: dict[str, Any]
    samples: list[list[Any]]
    stage_uuids: frozenset[str]


def _night_row(
    start: datetime, end: datetime, source: str | None, samples: list[Sample], day: str
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "day": day,
        "start": round(start.timestamp(), 3),
        "end": round(end.timestamp(), 3),
    }
    if source:
        row["source"] = source
    return row | summarize(timeline(samples, source))


def _read_record(record: Any, tz: tzinfo) -> _Incoming | None:
    if not isinstance(record, dict) or is_own_record(record):
        return None
    end = _instant(record.get("session_end_time"))
    if end is None:
        return None
    raw = record.get("duration_seconds")
    number = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
    samples = parse_samples(record.get("stages"))
    seconds = _finite(raw)
    if seconds is not None and seconds > 0:
        start = end - timedelta(seconds=seconds)
    elif samples:
        start = datetime.fromtimestamp(samples[0].start, UTC)
    else:
        start = end
    day = end.astimezone(tz).date().isoformat()
    source = _text(record.get("source"))
    return _Incoming(
        identity=session_identity(record.get("uuid"), end, number),
        day=day,
        row=_night_row(start, end, source, samples, day),
        samples=_sample_rows(samples),
        stage_uuids=frozenset(sample.uuid for sample in samples if sample.uuid),
    )


class NightFold:
    """One payload's sleep records and deletions, folded into the kept nights.

    In three steps around the generic session sum in history.py, in this order:
    `delete`, then `stale` tells which records that sum must leave alone, then `take`.
    Deletions come first, because an older Android app sends one uuid as both a record
    and a deletion, and the record has to survive that. `sum_days` afterwards names the
    days of the running sum that changed here rather than in the generic part.
    """

    def __init__(
        self,
        store: dict[str, Any],
        sessions: dict[str, dict[str, list]],
        records: Any,
        *,
        tz: tzinfo,
        at: float | None,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._tz = tz
        self._at = at
        self._before: dict[str, dict[str, float]] = {}
        incoming = records if isinstance(records, list) else []
        self._incoming = [
            item for item in (_read_record(record, tz) for record in incoming) if item is not None
        ]
        self._identities = {item.identity for item in self._incoming}
        self.sum_days: set[str] = set()
        self.changed = False

    @property
    def _nights(self) -> dict[str, dict[str, Any]]:
        return self._store.setdefault("nights", {})

    @property
    def _held(self) -> dict[str, list[list[Any]]]:
        return self._store.setdefault("stages", {})

    @property
    def _gone(self) -> dict[str, float]:
        return self._store.setdefault("gone", {})

    @property
    def _sums(self) -> dict[str, list]:
        return self._sessions.get(SLEEP_SESSIONS, {})

    def _touch(self, day: str) -> None:
        """Note a wake date's statistics as they stand, before anything of it changes."""
        self._before.setdefault(day, night_values(self._store, day))

    def _newer_than_payload(self, row: dict[str, Any] | None) -> bool:
        return (row is not None and self._at is not None and row.get("at") is not None) and row[
            "at"
        ] > self._at

    def delete(self, entries: Any) -> None:
        """The deletions of a payload: of a session on Android, of one stage on an iPhone."""
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "sleep":
                continue
            uuid = entry.get("uuid")
            if not isinstance(uuid, str) or not uuid:
                continue
            if uuid in self._nights or uuid in self._sums:
                if self._newer_than_payload(self._nights.get(uuid)):
                    continue
                self._drop_session(uuid)
                if self._at is not None:
                    self._gone[uuid] = self._at
                continue
            owner = self._stage_owner(uuid)
            if owner is not None and not self._newer_than_payload(self._nights.get(owner)):
                self._drop_stage(owner, uuid)

    def _stage_owner(self, uuid: str) -> str | None:
        for identity, rows in self._held.items():
            if any(len(row) > 3 and row[3] == uuid for row in rows):
                return identity
        return None

    def _drop_session(self, identity: str) -> None:
        row = self._nights.get(identity)
        if row is not None:
            self._touch(row["day"])
            del self._nights[identity]
        self._held.pop(identity, None)
        amount = self._sums.pop(identity, None)
        if amount is not None:
            self.sum_days.add(amount[0])
        self.changed = True

    def _drop_stage(self, identity: str, uuid: str) -> None:
        """Take a stage out of its night, whose figures are then what remains of it."""
        remaining = [row for row in self._held[identity] if len(row) <= 3 or row[3] != uuid]
        if not remaining:
            self._drop_session(identity)
            if self._at is not None:
                self._gone[identity] = self._at
            return
        samples = _samples(remaining)
        start = min(sample.start for sample in samples)
        end = max(sample.end for sample in samples)
        old = self._nights.get(identity)
        moment = datetime.fromtimestamp(end, UTC)
        day = moment.astimezone(self._tz).date().isoformat()
        if old is not None:
            self._touch(old["day"])
            self._touch(day)
            row = _night_row(
                datetime.fromtimestamp(start, UTC), moment, old.get("source"), samples, day
            )
            if (at := self._at if self._at is not None else old.get("at")) is not None:
                row["at"] = at
            self._nights[identity] = row
        self._held[identity] = remaining
        self.changed = True
        previous = self._sums.get(identity)
        if previous is not None:
            self._sums[identity] = [day, (end - start) / 60]
            self.sum_days.update({previous[0], day})

    def stale(self) -> set[str]:
        """The records of this payload that are older than what is already known of them.

        Older by the payload's own moment: a version replaced since, or a session deleted
        since. Or the same night in part: a session whose stage uuids are all in a larger
        one kept under another identity is an earlier state of that night. An iPhone
        session's uuid follows its earliest sample, so a night that grows can come under a
        new uuid, and a record from the outbox can still carry the old one. Sessions this
        payload sends itself do not count: they are about to be replaced.
        """
        stale: set[str] = set()
        for item in self._incoming:
            if self._at is not None:
                gone = self._gone.get(item.identity)
                if gone is not None and gone > self._at:
                    stale.add(item.identity)
                    continue
                known = self._nights.get(item.identity)
                if (
                    known is not None
                    and self._newer_than_payload(known)
                    and (
                        _without_at(known) != item.row
                        or self._held.get(item.identity, []) != item.samples
                    )
                ):
                    stale.add(item.identity)
                    continue
            if item.stage_uuids and any(
                item.stage_uuids < self._held_uuids(other)
                for other in self._held
                if other not in self._identities
            ):
                stale.add(item.identity)
        return stale

    def _held_uuids(self, identity: str) -> set[str]:
        return {row[3] for row in self._held.get(identity, []) if len(row) > 3 and row[3]}

    def take(self, stale: Collection[str]) -> None:
        """The readable records of the payload, each replacing what its identity held.

        Two sessions that share a stage are one night (`stale`), so an older one is
        retired; a night of the payload's own is not, it is replaced in the same pass.
        Nights before what the ledger keeps are not taken in: their rows stay as written,
        and none of it is a sum, so nothing has to be carried or corrected.
        """
        kept_from = self._store.get("kept_from")
        for item in self._incoming:
            if item.identity in stale or (kept_from and item.day < kept_from):
                continue
            existing = self._nights.get(item.identity)
            if (
                existing is not None
                and _without_at(existing) == item.row
                and self._held.get(item.identity, []) == item.samples
            ):
                continue
            self._touch(item.day)
            if existing is not None:
                self._touch(existing["day"])
            for other in [
                other
                for other in self._held
                if other not in self._identities and item.stage_uuids & self._held_uuids(other)
            ]:
                self._drop_session(other)
            row = dict(item.row)
            if self._at is not None:
                row["at"] = self._at
            self._nights[item.identity] = row
            if item.samples:
                self._held[item.identity] = item.samples
            else:
                self._held.pop(item.identity, None)
            self._gone.pop(item.identity, None)
            self.changed = True
        if self.changed:
            self._trim_held()

    def _trim_held(self) -> None:
        days = sorted(
            {self._nights[key]["day"] for key in self._held if key in self._nights}, reverse=True
        )
        kept = set(days[:KEPT_NIGHTS])
        for key in list(self._held):
            if key not in self._nights or self._nights[key]["day"] not in kept:
                del self._held[key]

    def rows(self) -> dict[str, dict[str, float | None]]:
        """What the payload changed in the statistics: day -> key -> hours.

        None for a value a date no longer has: its row is taken away. Only what changed,
        so a night sent again writes nothing and an older night only its own date.
        """
        changes: dict[str, dict[str, float | None]] = {}
        for day, before in self._before.items():
            after = night_values(self._store, day)
            changed = {
                key: after.get(key)
                for key in before.keys() | after.keys()
                if before.get(key) != after.get(key)
            }
            if changed:
                changes[day] = changed
        return changes


def _without_at(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "at"}


def noon(day: date, tz: tzinfo) -> datetime:
    """Where a wake date's row starts: local noon, on the hour in UTC.

    Noon rather than midnight, so the row stays on its own date in a day, week or month
    graph whatever the offset; in UTC, because a half-hour zone's noon is not on the hour.
    """
    return (
        datetime.combine(day, time(12), tzinfo=tz)
        .astimezone(UTC)
        .replace(minute=0, second=0, microsecond=0)
    )


def night_rows(
    changes: dict[str, dict[str, float | None]], *, tz: tzinfo
) -> dict[str, list[dict[str, Any]]]:
    """The statistics rows of what `NightFold.rows` found. A None takes a row away."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for iso in sorted(changes):
        start = noon(date.fromisoformat(iso), tz)
        for key, value in changes[iso].items():
            rows.setdefault(key, []).append(
                {"start": start, "mean": value, "min": value, "max": value}
            )
    return rows


def prune_nights(store: dict[str, Any], *, before: date) -> bool:
    """Let the nights before a date go, and take in nothing older from then on. A row
    already written stays in the recorder. Returns whether anything went."""
    cutoff = before.isoformat()
    nights = store.get("nights", {})
    old = [identity for identity, row in nights.items() if row["day"] < cutoff]
    for identity in old:
        del nights[identity]
        store.get("stages", {}).pop(identity, None)
    boundary = datetime.combine(before, time.min, tzinfo=UTC).timestamp()
    gone = store.get("gone", {})
    expired = [identity for identity, at in gone.items() if at < boundary]
    for identity in expired:
        del gone[identity]
    if old and store.get("kept_from", "") < cutoff:
        store["kept_from"] = cutoff
    return bool(old or expired)


def last_night(store: dict[str, Any], *, today: date | None = None) -> Night | None:
    """The main session of the newest wake date that has one.

    Never later than today, so a record with a wrong date in the future cannot hide the
    nights before it.
    """
    limit = today.isoformat() if today is not None else None
    days = {
        row["day"]
        for row in store.get("nights", {}).values()
        if row.get("asleep") and (limit is None or row["day"] <= limit)
    }
    if not days:
        return None
    day = max(days)
    main = main_session(store, day)
    if main is None:
        return None
    identity, row = main
    samples = _samples(store.get("stages", {}).get(identity, []))
    return Night(
        day=day,
        start=datetime.fromtimestamp(row["start"], UTC),
        end=datetime.fromtimestamp(row["end"], UTC),
        source=row.get("source"),
        minutes={name: row[name] for name in NIGHT_STATISTICS.values() if name in row},
        segments=timeline(samples, row.get("source")),
    )


def stages_attribute(night: Night, tz: tzinfo) -> list[dict[str, str]]:
    """The timeline as the `stages` attribute has it, in the local time zone."""
    return [
        {
            "stage": segment.stage,
            "start": _local(segment.start, tz),
            "end": _local(segment.end, tz),
        }
        for segment in night.segments
    ]


def _local(moment: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(moment, UTC).astimezone(tz).isoformat(timespec="seconds")


def sleep_updates(
    store: dict[str, Any], *, tz: tzinfo, today: date | None = None
) -> list[SensorUpdate]:
    """The last-night sensors, from the kept nights, or nothing when there is no night."""
    night = last_night(store, today=today)
    if night is None:
        return []
    attributes: dict[str, Any] = {
        "night": night.day,
        "start": night.start.astimezone(tz).isoformat(timespec="seconds"),
        "end": night.end.astimezone(tz).isoformat(timespec="seconds"),
    }
    if night.source:
        attributes["source"] = night.source
    attributes |= {f"{name}_minutes": round(value, 1) for name, value in night.minutes.items()}
    attributes["stages"] = stages_attribute(night, tz)
    updates = [
        SensorUpdate(
            key=KEY_LAST_SLEEP_ASLEEP,
            value=tile_hours(night.minutes["asleep"]),
            measured_at=night.end,
            attributes=attributes,
        )
    ]
    for key, name in _LAST_NIGHT_KEYS.items():
        minutes = night.minutes.get(name)
        updates.append(
            SensorUpdate(
                key=key,
                # None, never 0, for a stage this night's source does not report.
                value=None if minutes is None else tile_hours(minutes),
                measured_at=night.end,
                attributes={"night": night.day},
            )
        )
    return updates


__all__ = [
    "KEPT_NIGHTS",
    "LAST_NIGHT_KEYS",
    "NIGHT_STATISTICS",
    "Night",
    "NightFold",
    "SLEEP_SESSIONS",
    "Sample",
    "Segment",
    "clean_store",
    "last_night",
    "main_session",
    "night_rows",
    "night_values",
    "noon",
    "parse_samples",
    "payload_moment",
    "prune_nights",
    "session_identity",
    "sleep_updates",
    "stages_attribute",
    "summarize",
    "timeline",
]
