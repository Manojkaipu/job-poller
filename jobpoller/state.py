"""What the poller has seen before, kept in one JSON file.

Every posting is remembered, not just matching ones, so changing the keywords later doesn't
suddenly report old postings as new. A board seen for the first time is recorded silently
(a baseline). A board that fails to fetch keeps its postings untouched, so an outage is never
mistaken for everything being removed and then re-posted.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field

FORGET_AFTER_DAYS = 45  # a posting gone this long is forgotten; if it comes back it's reported again


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class BoardResult:
    key: str
    ok: bool
    job_ids: list[str] = field(default_factory=list)
    error: str | None = None


class State:
    def __init__(self, path: str):
        self.path = path
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        # boards: key -> {"first_run", "last_ok", "failures", "last_error", "count"}
        self.boards: dict[str, dict] = data.get("boards", {})
        # jobs: board key -> {job id -> [first_seen, last_seen]}
        self.jobs: dict[str, dict[str, list[str]]] = data.get("jobs", {})

    def is_baseline(self, board: str) -> bool:
        return board not in self.boards or "last_ok" not in self.boards[board]

    def new_ids(self, board: str, ids: list[str]) -> set[str]:
        known = self.jobs.get(board, {})
        return {i for i in ids if i not in known}

    def record(self, r: BoardResult, when: str | None = None) -> None:
        when = when or now()
        b = self.boards.setdefault(r.key, {"first_run": when, "failures": 0})
        if not r.ok:
            b["failures"] = b.get("failures", 0) + 1
            b["last_error"] = r.error
            return
        prev = b.get("count")
        b.update(last_ok=when, failures=0, count=len(r.job_ids), prev_count=prev)
        b.pop("last_error", None)
        seen = self.jobs.setdefault(r.key, {})
        for i in r.job_ids:
            if i in seen:
                seen[i][1] = when
            else:
                seen[i] = [when, when]

    def prune(self, when: str | None = None, keep_boards: set[str] | None = None) -> int:
        cutoff = (dt.datetime.fromisoformat(when or now()) - dt.timedelta(days=FORGET_AFTER_DAYS)).isoformat()
        dropped = 0
        for board, seen in list(self.jobs.items()):
            if keep_boards is not None and board not in keep_boards:
                dropped += len(seen)  # board removed from the config
                del self.jobs[board]
                self.boards.pop(board, None)
                continue
            for i in [i for i, (_, last) in seen.items() if last < cutoff]:
                del seen[i]
                dropped += 1
        return dropped

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "boards": self.boards, "jobs": self.jobs}, f, separators=(",", ":"))
        os.replace(tmp, self.path)  # atomic, so a crash mid-write can't corrupt the state
