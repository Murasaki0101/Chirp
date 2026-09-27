"""Message sources: DWS (real DingTalk) and Mock (offline development).

The DWS field names below are best-effort guesses based on the dingtalk-chat
skill docs. When you first run against a real authenticated connector, inspect
the raw JSON dumped to `.cache/last_unread.json` and adjust `_parse_*` if any
field name doesn't line up. Everything else in the app is field-name agnostic.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Protocol

from .jev_client import get_env_path

log = logging.getLogger(__name__)

CACHE_DIR = get_env_path().parent / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Message:
    conversation_id: str          # openConversationId — stable ID for the chat
    conversation_name: str        # display name of the chat / peer
    is_group: bool
    sender_name: str
    sender_id: str
    content: str
    timestamp_ms: int
    unread_count: int = 0
    message_id: str = ""
    deep_link: str = ""           # URL to open this chat in the DingTalk client
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity used to dedupe / hide after click."""
        return self.message_id or f"{self.conversation_id}:{self.timestamp_ms}"


# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------

class Source(Protocol):
    def fetch(self) -> list[Message]:
        ...


# ---------------------------------------------------------------------------
# Deep-link builder
# ---------------------------------------------------------------------------

def build_deep_link(
    conversation_id: str = "",
    message_id: str = "",
    raw: dict[str, Any] | None = None,
) -> str:
    """Best-effort deep link into the DingTalk desktop client.

    Priority:
    1. Any URL-like field the DWS response already gives us.
    2. `dingtalk://dingtalkclient/action/open_conversation?conversation_id=...`
       (opens the chat; may not scroll to a specific message, but that's OK —
       the freshest unread is at the bottom anyway).
    3. Bare `dingtalk://` to at least activate the client.
    """
    raw = raw or {}
    # 1) look for a ready-made URL in the payload
    for k in ("url", "link", "deepLink", "deep_link", "messageUrl",
              "conversationUrl", "jumpUrl", "openLink"):
        v = raw.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://", "dingtalk://")):
            return v

    if conversation_id:
        q = urllib.parse.urlencode({"conversation_id": conversation_id})
        base = f"dingtalk://dingtalkclient/action/open_conversation?{q}"
        if message_id:
            base += "&message_id=" + urllib.parse.quote(message_id)
        return base
    return "dingtalk://"


# ---------------------------------------------------------------------------
# DWS source
# ---------------------------------------------------------------------------

class DwsSource:
    """Fetch unread chats via `dws chat +unread-chats`, then hydrate each with
    its latest non-self message via `dws chat +chat-messages`.

    Set `DWS_TIMEOUT` (seconds) to override the per-command timeout.
    """

    def __init__(
        self,
        count: int = 100,
        exclude_muted: bool = False,
        hydrate_content: bool = True,
        timeout: float | None = None,
        self_id: str | None = None,
        dws_bin: str | None = None,
    ) -> None:
        self.count = count
        self.exclude_muted = exclude_muted
        self.hydrate_content = hydrate_content
        self.timeout = timeout or float(os.environ.get("DWS_TIMEOUT", "20"))
        # Your own senderId, used to skip messages you sent yourself.
        self.self_id = self_id or os.environ.get("RADAR_SELF_ID", "")
        # Allow overriding the dws binary path (e.g. npm global install location)
        self.dws_bin = dws_bin or os.environ.get("DWS_BIN", "dws")

    # ------------------------------------------------------------------

    def _run(self, args: list[str]) -> dict[str, Any] | None:
        cmd = [self.dws_bin, *args, "--format", "json"]
        log.debug("DWS exec: %s", " ".join(shlex.quote(c) for c in cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError:
            log.error(
                "`%s` not found in PATH. Install the official DingTalk CLI: "
                "`npm i -g dingtalk-workspace-cli`, then `dws auth login`.",
                self.dws_bin,
            )
            return None
        except subprocess.TimeoutExpired:
            log.error("dws timeout after %.1fs: %s", self.timeout, " ".join(cmd))
            return None

        if proc.returncode != 0:
            log.error("dws failed (exit %d): %s", proc.returncode, proc.stderr.strip()[:500])
            return None

        out = proc.stdout.strip()
        # Detect the QwenWork host-shim placeholder: it means `dws` on PATH is
        # QwenWork's wrapper, not the standalone official CLI. The standalone
        # app must call the real npm-installed dws binary.
        if "pending-post-tool-use" in out or "pending-host-side" in out:
            log.error(
                "dws returned a QwenWork host-shim placeholder. For standalone "
                "use, install the official CLI (npm i -g dingtalk-workspace-cli) "
                "and set DWS_BIN to its path."
            )
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            log.error("dws returned non-JSON: %s (%s)", out[:200], e)
            return None

    # ------------------------------------------------------------------

    def fetch(self) -> list[Message]:
        unread = self._fetch_unread_chats()
        if not unread:
            return []
        if not self.hydrate_content:
            return unread

        # Hydrate each conversation with its most recent message so we have
        # real content for Jev to analyze. Sequential is fine for ~30 chats;
        # bump with a thread pool if you raise `count`.
        hydrated: list[Message] = []
        for m in unread:
            latest = self._fetch_latest_message(m.conversation_id, is_group=m.is_group)
            if latest is not None:
                latest.conversation_name = m.conversation_name
                latest.unread_count = m.unread_count
                latest.sender_name = latest.sender_name or m.sender_name
                hydrated.append(latest)
            else:
                hydrated.append(m)
        return hydrated

    # ------------------------------------------------------------------

    def _fetch_unread_chats(self) -> list[Message]:
        args = ["chat", "+unread-chats", "--count", str(self.count)]
        if self.exclude_muted:
            args.append("--exclude-muted")
        data = self._run(args)
        if data is None:
            raise RuntimeError("未能连接钉钉。请安装官方 dws 并登录，或在设置中指定已登录的 dws 路径。")
        # dump for debugging / field discovery
        try:
            (CACHE_DIR / "last_unread.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

        conversations = _extract_list(data, [
            ("result", "conversations"),   # +unread-chats
            ("data", "conversations"),     # +recent-conversations
            ("data", "chats"),
            ("data", "items"),
            ("conversations",),
            ("chats",),
            ("items",),
            ("result",),
            ("data",),
        ])
        out: list[Message] = []
        for c in conversations:
            m = self._parse_conversation(c)
            if m is not None:
                out.append(m)
        return out

    def _parse_conversation(self, c: dict[str, Any]) -> Message | None:
        cid = _first_str(c, ["conversationId", "openConversationId", "cid", "id"])
        if not cid:
            return None
        name = _first_str(c, ["name", "title", "conversationName", "displayName"]) or "(未知会话)"
        # DingTalk uses "direct" / "group" / "unknown" for `type`.
        ctype = _first_str(c, ["type", "conversationType", "chatType"]).lower()
        is_group = ctype in ("group", "2") or c.get("isGroup") is True
        unread = int(_first_num(c, ["unreadCount", "unread", "redPointCount"]) or 0)
        ts = _parse_ts(_first_any(c, [
            "latestMessageTime", "lastMessageTime", "updateTime", "timestamp",
        ]))
        last_content = _first_str(c, [
            "latestMessageContent", "lastMessageContent", "latestMessage",
            "preview", "summary", "content", "text",
        ])
        last_sender = _first_str(c, ["latestMessageSenderName", "lastSenderName",
                                      "senderName", "sender"])
        last_msg_id = _first_str(c, ["latestMessageId", "lastMessageId", "messageId"])

        raw = dict(c)
        return Message(
            conversation_id=cid,
            conversation_name=name,
            is_group=is_group,
            sender_name=last_sender or name,
            sender_id=_first_str(c, ["latestMessageSenderId", "senderId"]) or "",
            content=last_content or "",
            timestamp_ms=ts,
            unread_count=unread,
            message_id=last_msg_id,
            deep_link=build_deep_link(cid, last_msg_id, raw),
            raw=raw,
        )

    # ------------------------------------------------------------------

    def _fetch_latest_message(self, cid: str, is_group: bool) -> Message | None:
        # NOTE: `--page-limit` is only valid together with `--page-all`, so we
        # omit it and rely on the default single page (which is enough to get
        # the most recent few messages). Both group and direct chats accept
        # `--group <conversationId>` (verified against the real API).
        args = ["chat", "+chat-messages", "--group", cid,
                "--order", "desc", "--no-reactions"]
        data = self._run(args)
        if data is None:
            return None
        msgs = _extract_list(data, [
            ("messages",),                  # real +chat-messages shape
            ("data", "messages"),
            ("data", "items"),
            ("items",),
        ])
        if not msgs:
            return None
        # Latest non-self message (skip ones the current user sent)
        for m in msgs:
            if _is_self(m, self.self_id):
                continue
            return self._parse_message(m, fallback_cid=cid, is_group=is_group)
        # all recent messages are self — still return the latest so radar sees activity
        return self._parse_message(msgs[0], fallback_cid=cid, is_group=is_group)

    def _parse_message(self, m: dict[str, Any], fallback_cid: str, is_group: bool) -> Message:
        cid = _first_str(m, ["conversationId", "openConversationId"]) or fallback_cid
        mid = _first_str(m, ["messageId", "openMessageId", "id"]) or ""
        content = _first_str(m, ["text", "content", "body", "plainText"]) or _stringify_content(m.get("content"))
        sender = _first_str(m, ["sender", "senderName", "senderNick", "fromName"]) or ""
        sender_id = _first_str(m, ["senderId", "senderUserId", "fromUserId"]) or ""
        ts = _parse_ts(_first_any(m, ["createTime", "sendTime", "timestamp", "messageTime"]))
        conv_name = _first_str(m, ["conversationName", "groupName", "chatName"]) or ""
        return Message(
            conversation_id=cid,
            conversation_name=conv_name,
            is_group=is_group,
            sender_name=sender,
            sender_id=sender_id,
            content=content,
            timestamp_ms=ts,
            message_id=mid,
            deep_link=build_deep_link(cid, mid, m),
            raw=m,
        )


# ---------------------------------------------------------------------------
# Cache source — the primary path for the standalone app
# ---------------------------------------------------------------------------
#
# IMPORTANT ARCHITECTURE NOTE:
# The `dws` binary is a QwenWork shim that routes commands to the host app via
# a PostToolUse hook. It ONLY works when invoked directly by QwenWork's Bash
# tool — a standalone Python process (or even a Python subprocess nested inside
# the Bash tool) gets back a `[dws-bash:pending-post-tool-use]` placeholder
# instead of real data.
#
# Therefore the floating window app CANNOT call dws directly. Instead:
#   1. A refresh step (run inside QwenWork's Bash tool, or via a QwenWork cron
#      job) executes the dws commands and writes `.cache/inbox.json`.
#   2. The app reads `.cache/inbox.json` via CacheSource.
#
# See scripts/build_inbox.py for the refresh-side helper, and README.md for
# how to schedule it.

INBOX_FILE = CACHE_DIR / "inbox.json"


class CacheSource:
    """Reads a pre-built inbox.json produced by the QwenWork-side refresh step.

    Expected schema::

        {
          "fetched_at": "2026-09-27T13:45:00+08:00",   # optional
          "messages": [
            {
              "conversation_id": "...",
              "conversation_name": "...",
              "is_group": false,
              "sender_name": "...",
              "sender_id": "...",
              "content": "...",
              "timestamp_ms": 1790000000000,
              "unread_count": 1,
              "message_id": "...",
              "deep_link": "dingtalk://..."   # optional, auto-built if missing
            }
          ]
        }
    """

    def __init__(self, path: Path | str = INBOX_FILE) -> None:
        self.path = Path(path)
        self.fetched_at = ""

    def fetch(self) -> list[Message]:
        if not self.path.exists():
            log.warning("inbox cache not found at %s — run scripts/build_inbox.py "
                        "inside QwenWork first, or set RADAR_MOCK=true", self.path)
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.error("failed to read inbox cache %s: %s", self.path, e)
            return []

        raw_msgs = data.get("messages") if isinstance(data, dict) else data
        self.fetched_at = data.get("fetched_at", "") if isinstance(data, dict) else ""
        if not isinstance(raw_msgs, list):
            log.error("inbox cache has no 'messages' list")
            return []

        out: list[Message] = []
        for m in raw_msgs:
            if not isinstance(m, dict):
                continue
            cid = str(m.get("conversation_id", ""))
            if not cid:
                continue
            mid = str(m.get("message_id", ""))
            ts = m.get("timestamp_ms")
            if isinstance(ts, str):
                ts = _parse_ts(ts)
            out.append(Message(
                conversation_id=cid,
                conversation_name=str(m.get("conversation_name", "(未知)")),
                is_group=bool(m.get("is_group", False)),
                sender_name=str(m.get("sender_name", "")),
                sender_id=str(m.get("sender_id", "")),
                content=str(m.get("content", "")),
                timestamp_ms=int(ts or 0),
                unread_count=int(m.get("unread_count", 0) or 0),
                message_id=mid,
                deep_link=str(m.get("deep_link", "")) or build_deep_link(cid, mid, m),
                raw=m,
            ))
        return out


# ---------------------------------------------------------------------------
# Mock source
# ---------------------------------------------------------------------------

MOCK_MESSAGES: list[dict[str, Any]] = [
    {
        "conversation_id": "cidMOCK1",
        "conversation_name": "老王",
        "is_group": False,
        "sender_name": "老王",
        "content": "在吗？下午3点前把上周的销售数据发我，老板要看，急。",
        "timestamp_ms": int(time.time() * 1000) - 5 * 60_000,
        "unread_count": 2,
    },
    {
        "conversation_id": "cidMOCK2",
        "conversation_name": "产品评审群",
        "is_group": True,
        "sender_name": "小李",
        "content": "@你 这个功能的验收标准能确认下吗？测试同学等着用例。",
        "timestamp_ms": int(time.time() * 1000) - 12 * 60_000,
        "unread_count": 5,
    },
    {
        "conversation_id": "cidMOCK3",
        "conversation_name": "客服小张",
        "is_group": False,
        "sender_name": "客服小张",
        "content": "有个客户投诉订单一直未发货，情绪比较激动，麻烦尽快看下。",
        "timestamp_ms": int(time.time() * 1000) - 25 * 60_000,
        "unread_count": 3,
    },
    {
        "conversation_id": "cidMOCK4",
        "conversation_name": "通知群",
        "is_group": True,
        "sender_name": "系统",
        "content": "【通知】本周五下午 4 点全员大会，请准时参加。",
        "timestamp_ms": int(time.time() * 1000) - 60 * 60_000,
        "unread_count": 1,
    },
    {
        "conversation_id": "cidMOCK5",
        "conversation_name": "老婆",
        "is_group": False,
        "sender_name": "老婆",
        "content": "今晚回家吃饭吗？",
        "timestamp_ms": int(time.time() * 1000) - 90 * 60_000,
        "unread_count": 1,
    },
    {
        "conversation_id": "cidMOCK6",
        "conversation_name": "项目协作群",
        "is_group": True,
        "sender_name": "PM 阿强",
        "content": "刚才那个 bug 复现了，日志我发你，麻烦帮忙看下是不是上次改动引入的。",
        "timestamp_ms": int(time.time() * 1000) - 3 * 60_000,
        "unread_count": 8,
    },
]


class MockSource:
    """Deterministic offline source for UI development.

    Reads from `MOCK_MESSAGES` by default. Set env `RADAR_MOCK_FILE=/path.json`
    to load a custom fixture (list of message dicts).
    """

    def __init__(self, messages: Iterable[dict[str, Any]] | None = None) -> None:
        override = os.environ.get("RADAR_MOCK_FILE")
        if messages is None and override and Path(override).exists():
            messages = json.loads(Path(override).read_text(encoding="utf-8"))
        self._messages = list(messages or MOCK_MESSAGES)

    def fetch(self) -> list[Message]:
        out: list[Message] = []
        for m in self._messages:
            cid = m.get("conversation_id", "")
            mid = m.get("message_id", "")
            out.append(Message(
                conversation_id=cid,
                conversation_name=m.get("conversation_name", "(mock)"),
                is_group=bool(m.get("is_group", False)),
                sender_name=m.get("sender_name", ""),
                sender_id=m.get("sender_id", ""),
                content=m.get("content", ""),
                timestamp_ms=int(m.get("timestamp_ms", time.time() * 1000)),
                unread_count=int(m.get("unread_count", 1)),
                message_id=mid,
                deep_link=build_deep_link(cid, mid, m),
                raw=m,
            ))
        return out


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _extract_list(data: Any, paths: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    """Walk `paths` in order; return the first list-of-dicts we find."""
    for path in paths:
        cur = data
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok:
            if isinstance(cur, list):
                return [x for x in cur if isinstance(x, dict)]
            if isinstance(cur, dict):
                # sometimes wrapped one more level
                for v in cur.values():
                    if isinstance(v, list):
                        return [x for x in v if isinstance(x, dict)]
    return []


def _first_str(d: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, (int, float)) and v:
            return str(v)
    return ""


def _first_any(d: dict[str, Any], keys: list[str]) -> Any:
    """Return the first present, non-None value (preserving original type)."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _first_num(d: dict[str, Any], keys: list[str]) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return None


# DingTalk returns time in several shapes depending on the endpoint:
#   - "+recent-conversations": "2026-09-27T13:13:25+08:00"  (ISO-8601 w/ tz)
#   - "+chat-messages":        "2026-09-27 13:13:25"        (naive, local tz)
#   - some endpoints:          1790000000000                 (epoch ms)
_TS_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_ts(value: Any) -> int:
    """Normalize any DingTalk time value to epoch milliseconds. Returns 0 on failure."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        # heuristic: seconds vs milliseconds
        return int(v * 1000) if v < 1e12 else int(v)
    if not isinstance(value, str):
        return 0
    s = value.strip()
    if not s:
        return 0
    # pure digits → epoch
    if re.fullmatch(r"\d{10,13}", s):
        v = float(s)
        return int(v * 1000) if v < 1e12 else int(v)
    for fmt in _TS_PATTERNS:
        try:
            dt = datetime.strptime(s, fmt)
            # naive datetime → assume local time (DingTalk server is Asia/Shanghai)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    # last resort: fromisoformat handles most ISO-8601
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def _is_self(m: dict[str, Any], self_id: str = "") -> bool:
    """True if this message was sent by the current user.

    The official dws CLI does not flag self-sent messages, so we compare
    senderId against RADAR_SELF_ID. Also honor any explicit boolean flags.
    """
    if self_id and m.get("senderId") == self_id:
        return True
    for k in ("isSelf", "selfSent", "fromSelf"):
        if m.get(k) is True:
            return True
    return False


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Common shapes: {"text": "..."} or {"content": "..."} or {"type":"text","value":"..."}
        for k in ("text", "content", "value", "plainText"):
            v = content.get(k)
            if isinstance(v, str) and v:
                return v
        return json.dumps(content, ensure_ascii=False)[:400]
    if isinstance(content, list):
        return " ".join(_stringify_content(x) for x in content)
    return str(content)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _official_dws_available() -> bool:
    """True if a STANDALONE official dws CLI is on PATH (not QwenWork's shim).

    QwenWork wraps dws as a host-routing shim under ~/.qwenworkcn/bin; that one
    only works inside QwenWork's Bash tool. The standalone plugin needs the
    official npm-installed binary (npm i -g dingtalk-workspace-cli).
    """
    import shutil
    dws = os.environ.get("DWS_BIN", "dws")
    path = shutil.which(dws)
    if not path:
        return False
    if ".qwenworkcn" in path:   # QwenWork shim — not usable standalone
        return False
    return True


def make_source(prefer_mock: bool | None = None) -> Source:
    """Pick the message source.

    - RADAR_MOCK=true            → MockSource (offline UI dev)
    - RADAR_SOURCE=dws|cache     → force that source
    - auto (default)             → official dws if available (standalone main
                                    path), else inbox cache, else DwsSource
                                    (which will log a clear install hint).
    """
    if prefer_mock is None:
        prefer_mock = os.environ.get("RADAR_MOCK", "").lower() in ("1", "true", "yes")
    if prefer_mock:
        log.info("Using MockSource (RADAR_MOCK enabled)")
        return MockSource()

    src = os.environ.get("RADAR_SOURCE", "").strip().lower()
    inbox = Path(os.environ.get("RADAR_INBOX", str(INBOX_FILE)))

    if src == "dws":
        log.info("Using DwsSource (RADAR_SOURCE=dws)")
        return DwsSource()
    if src == "cache":
        log.info("Using CacheSource (%s)", inbox)
        return CacheSource(inbox)

    # auto
    if _official_dws_available():
        log.info("Using DwsSource (official standalone dws detected)")
        return DwsSource()
    if inbox.exists():
        log.info("Using CacheSource (%s) — official dws not on PATH", inbox)
        return CacheSource(inbox)
    log.warning("No official dws on PATH and no inbox cache. Using DwsSource; "
                "install with `npm i -g dingtalk-workspace-cli` + `dws auth login`.")
    return DwsSource()
