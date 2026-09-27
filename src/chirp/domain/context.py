"""User context — the "my current situation" side of urgency.

Urgency is not a property of a message alone; it's a function of the message
AND the reader's situation. The same "send it before 3pm" is trivial at 9am on
a free day, and critical at 2:50pm when you're in back-to-back meetings.

This module loads that situation (calendar + todos + current time) from a
cache file produced by the QwenWork-side refresh step, and serializes it into
text that gets folded into Jev's `state` so every judgment is context-aware.

Cache file: `.cache/context.json` (see README and `scripts/demo_context.py`).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..infrastructure.jev_client import get_env_path

log = logging.getLogger(__name__)

CONTEXT_FILE = get_env_path().parent / ".cache" / "context.json"

# DingTalk todo priority codes
PRIORITY_LABEL = {10: "低", 20: "普通", 30: "高", 40: "紧急"}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TodoItem:
    task_id: str
    title: str
    due_ms: int = 0
    priority: int = 20

    @property
    def priority_label(self) -> str:
        return PRIORITY_LABEL.get(self.priority, str(self.priority))

    @property
    def is_overdue(self) -> bool:
        return self.due_ms > 0 and self.due_ms < time.time() * 1000


@dataclass
class CalendarEvent:
    event_id: str
    title: str
    start_ms: int
    end_ms: int

    def contains(self, ms: int) -> bool:
        return self.start_ms <= ms < self.end_ms

    def fmt_range(self) -> str:
        s = datetime.fromtimestamp(self.start_ms / 1000).strftime("%H:%M")
        e = datetime.fromtimestamp(self.end_ms / 1000).strftime("%H:%M")
        return f"{s}-{e}"


@dataclass
class UserContext:
    now_ms: int
    today_events: list[CalendarEvent] = field(default_factory=list)
    next_event: CalendarEvent | None = None
    due_today_todos: list[TodoItem] = field(default_factory=list)
    open_todos: list[TodoItem] = field(default_factory=list)
    fetched_at: str = ""

    # ------------------------------------------------------------------

    @property
    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.now_ms / 1000)

    def current_event(self) -> CalendarEvent | None:
        """The event happening right now, if any."""
        for ev in self.today_events:
            if ev.contains(self.now_ms):
                return ev
        return None

    def is_busy_now(self) -> bool:
        return self.current_event() is not None

    def next_upcoming_event(self) -> CalendarEvent | None:
        """The next event starting after now (within today)."""
        upcoming = [ev for ev in self.today_events if ev.start_ms > self.now_ms]
        if upcoming:
            return min(upcoming, key=lambda e: e.start_ms)
        return self.next_event

    def free_until_ms(self) -> int | None:
        """If busy now, when does the current event end? Else None."""
        ev = self.current_event()
        return ev.end_ms if ev else None

    def minutes_until_next_event(self) -> float | None:
        ev = self.next_upcoming_event()
        if ev is None:
            return None
        return (ev.start_ms - self.now_ms) / 60000.0

    # ------------------------------------------------------------------

    def serialize(self) -> str:
        """Render the context as compact Chinese text for Jev's state block."""
        dt = self.now_dt
        lines = [f"【当前时间】{dt.strftime('%Y-%m-%d %H:%M')}（{WEEKDAY_CN[dt.weekday()]}）"]

        # Calendar
        cur = self.current_event()
        if cur:
            lines.append(f"【正在进行】{cur.fmt_range()} {cur.title}（我现在在开会/忙，无法立即处理其他事）")
        if self.today_events:
            ev_str = "；".join(f"{e.fmt_range()} {e.title}" for e in
                               sorted(self.today_events, key=lambda e: e.start_ms))
            lines.append(f"【今日日程】{ev_str}")
        else:
            lines.append("【今日日程】无（今天空闲）")

        nxt = self.next_upcoming_event()
        if nxt and not (cur and nxt.event_id == cur.event_id):
            mins = self.minutes_until_next_event()
            soon = f"（{int(mins)}分钟后开始）" if mins is not None and mins <= 120 else ""
            lines.append(f"【接下来】{nxt.fmt_range()} {nxt.title}{soon}")

        # Todos
        if self.due_today_todos:
            t = "；".join(f"{x.title}[{x.priority_label}]" for x in self.due_today_todos[:5])
            lines.append(f"【今天到期待办】{t}")
        overdue = [x for x in self.open_todos if x.is_overdue]
        if overdue:
            t = "；".join(f"{x.title}[{x.priority_label}]" for x in overdue[:5])
            lines.append(f"【已逾期待办】{t}")
        if self.open_todos:
            t = "；".join(x.title for x in self.open_todos[:6])
            lines.append(f"【未完成待办】{t}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _parse_event(d: dict[str, Any]) -> CalendarEvent | None:
    eid = str(d.get("eventId") or d.get("id") or "")
    title = str(d.get("title") or d.get("summary") or d.get("subject") or "(日程)")
    start = _to_ms(d.get("start") or d.get("startTime") or d.get("startMs"))
    end = _to_ms(d.get("end") or d.get("endTime") or d.get("endMs"))
    if start == 0:
        return None
    if end == 0:
        end = start + 3600_000  # default 1h
    return CalendarEvent(event_id=eid, title=title, start_ms=start, end_ms=end)


def _parse_todo(d: dict[str, Any]) -> TodoItem | None:
    tid = str(d.get("taskId") or d.get("id") or "")
    title = str(d.get("title") or d.get("subject") or "")
    if not title:
        return None
    due = _to_ms(d.get("dueTime") or d.get("planFinishDate") or d.get("due"))
    prio = int(d.get("priority") or 20)
    return TodoItem(task_id=tid, title=title, due_ms=due, priority=prio)


def _to_ms(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        n = float(v)
        return int(n * 1000) if n < 1e12 else int(n)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            n = float(s)
            return int(n * 1000) if n < 1e12 else int(n)
        # ISO or "YYYY-MM-DD HH:MM:SS"
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(datetime.strptime(s, fmt).timestamp() * 1000)
            except ValueError:
                continue
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def load_context(path: Path | str | None = None, now_ms: int | None = None) -> UserContext:
    """Load context from cache. Always uses a fresh `now` (time moves on).

    If the cache is missing/corrupt, returns an empty context (the analyzer
    still works, just without situational awareness).
    """
    path = Path(path or os.environ.get("RADAR_CONTEXT", str(CONTEXT_FILE)))
    now = now_ms or int(time.time() * 1000)

    if not path.exists():
        log.info("no context cache at %s — running without situational context", path)
        return UserContext(now_ms=now)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("context cache unreadable (%s) — ignoring", e)
        return UserContext(now_ms=now)

    today_events = [e for e in (_parse_event(d) for d in (data.get("today_events") or [])) if e]
    next_event = _parse_event(data.get("next_event") or {}) if data.get("next_event") else None
    due_today = [t for t in (_parse_todo(d) for d in (data.get("due_today_todos") or [])) if t]
    open_todos = [t for t in (_parse_todo(d) for d in (data.get("open_todos") or [])) if t]

    return UserContext(
        now_ms=now,
        today_events=today_events,
        next_event=next_event,
        due_today_todos=due_today,
        open_todos=open_todos,
        fetched_at=str(data.get("fetched_at", "")),
    )


def empty_context(now_ms: int | None = None) -> UserContext:
    return UserContext(now_ms=now_ms or int(time.time() * 1000))
