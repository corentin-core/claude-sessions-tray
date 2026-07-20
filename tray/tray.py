#!/usr/bin/python3
"""GNOME tray applet: lists Claude Code sessions across all folders,
highlights the ones waiting for an instruction, and opens the VSCode
window for the folder on click.

Must run with /usr/bin/python3 (system PyGObject + AyatanaAppIndicator3).
Debug mode: `/usr/bin/python3 tray.py --once` prints the state as JSON and exits.
"""

import glob
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
POLL_SECONDS = 4
STREAMING_WINDOW = 8  # transcript written less than N s ago => a tool is running
WAITING_WINDOW = 900  # finished less than N s ago => waiting for you; beyond => idle
# Claude's turn but transcript silent for N s => 🔴 possibly stuck
# (hung tool, crashed CLI, loop; a legitimately long tool can also trigger it).
STUCK_WINDOW = 600
TAIL_BYTES = 131072
# Subscription quota: `claude -p /usage` is purely local (0 tokens, 0 cost),
# but spawning it takes ~1.6 s => queried rarely, in a thread, off the GTK loop.
USAGE_POLL_SECONDS = 180
USAGE_CMD_TIMEOUT = 30
# Delay between focus and deep link. Raise it if a click opens the wrong
# window, lower it for more responsiveness.
FOCUS_DELAY = 0

# Desktop notification when a session turns 🟠 ready. Set to False to turn it
# off. A session whose window already has focus is not notified.
NOTIFY_ON_READY = True

# Click behavior (none is perfect under Wayland, pick one):
#   True  -> targets the session by ID. Precise within the focused window;
#            opens a DUPLICATE if the session is in another window (VSCode
#            routes the deep link to the focused window, Wayland forbids
#            changing that).
#   False -> opens/raises the FOLDER's window (no duplicate, works across
#            windows); but you pick the tab, and 2 sessions of the same
#            folder are indistinguishable.
TARGET_SESSION_BY_ID = True

# Menu grouping by project: a folder with at least this many calm sessions
# (🔵/⚪) is collapsed into a "📁 folder (N)" submenu, otherwise the session
# stays flat. Sessions needing attention (🟠/🔴) always stay flat at the top.
PROJECT_SUBMENU_MIN = 2


def claude_bin():
    return shutil.which("claude") or max(
        glob.glob(f"{HOME}/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"),
        default="claude",
    )


def list_live_sessions():
    """Live interactive sessions, across all folders."""
    try:
        out = subprocess.run(
            [claude_bin(), "agents", "--json", "--all"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        ).stdout
        return json.loads(out or "[]")
    except Exception:
        return []


_USAGE_LINE = re.compile(
    r"\s*(Current [^:]+):\s*(\d+)% used(?:\s*·\s*resets\s+(.+?))?\s*$"
)
_USAGE_SHORT = {
    "Current session": "session",
    "Current week (all models)": "week",
    "Current week (Fable)": "Fable",
}


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}


def _parse_reset(text):
    """« Jul 17, 1:20pm (Europe/Paris) » -> epoch, or None. Manual parsing, no
    strptime: `%b`/`%p` depend on the locale (fr_FR expects "juil."). Local
    time: the tz shown by /usage is the machine's."""
    m = re.match(r"([A-Za-z]{3})\s+(\d+),\s*(\d+)(?::(\d+))?\s*([ap])m", text.strip(), re.I)
    if not m:
        return None
    mon, day, hour, minute, ap = m.groups()
    month = _MONTHS.get(mon.lower())
    if not month:
        return None
    hour = int(hour) % 12 + (12 if ap.lower() == "p" else 0)
    minute = int(minute or 0)
    now = datetime.now()
    for year in (now.year, now.year + 1):
        try:
            dt = datetime(year, month, int(day), hour, minute)
        except ValueError:
            return None
        if dt.timestamp() > now.timestamp() - 3600:  # tolerates a slight clock skew
            return dt.timestamp()
    return dt.timestamp()


def parse_usage(text):
    """The 3 quota lines from /usage -> [{label, short, pct, reset_epoch}]."""
    limits = []
    for line in text.splitlines():
        m = _USAGE_LINE.match(line)
        if not m:
            continue
        label, pct, reset = m.groups()
        label = label.strip()
        limits.append({
            "label": label,
            "short": _USAGE_SHORT.get(label, label),
            "pct": int(pct),
            "reset_epoch": _parse_reset(reset) if reset else None,
        })
    return limits


# Env vars that redirect claude to non-OAuth auth: with them set,
# /usage does not return subscription quotas.
_AUTH_OVERRIDE_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
)


def fetch_usage():
    """Subscription quota via `claude -p /usage`. Blocking (~1.6 s): call it
    off the GTK loop. -> list of limits, or None if the call fails."""
    env = {k: v for k, v in os.environ.items() if k not in _AUTH_OVERRIDE_VARS}
    try:
        out = subprocess.run(
            [claude_bin(), "-p", "/usage", "--output-format", "json"],
            capture_output=True, text=True, timeout=USAGE_CMD_TIMEOUT,
            stdin=subprocess.DEVNULL, env=env,
        ).stdout
        data = json.loads(out or "{}")
        if data.get("is_error"):
            return None
        return parse_usage(data.get("result", ""))
    except Exception as exc:
        print(f"[tray] fetch_usage failed: {exc!r}", file=sys.stderr)
        return None


def _tail_events(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # first line is often truncated by the seek
    return events


def _transcript_for(session_id):
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def _last_assistant(events):
    for e in reversed(events):
        if e.get("type") == "assistant":
            return e
    return None


def _last_error(events):
    """True if the last significant event is a failed tool_result, with
    Claude having produced nothing since. Refines the stuck state."""
    for e in reversed(events):
        t = e.get("type")
        if t == "assistant":
            return False
        if t == "user":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        return bool(c.get("is_error"))
            return False  # user prompt, not a tool_result
    return False


def _pending_question(events):
    """True if the last tool_use is an AskUserQuestion not yet answered
    (no tool_result after it): it's your turn to choose, not Claude's."""
    for e in reversed(events):
        t = e.get("type")
        if t == "user":
            content = (e.get("message") or {}).get("content")
            is_tool_result = "toolUseResult" in e or (
                isinstance(content, list)
                and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
            )
            if is_tool_result:
                return False  # an answer arrived after the question
        if t == "assistant":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                return any(
                    isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name") == "AskUserQuestion"
                    for c in content
                )
    return False


def _text_of(event):
    msg = event.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(t.strip() for t in parts if t.strip())
    if isinstance(content, str):
        return content.strip()
    return ""


def session_state(session):
    """-> dict(name, cwd, state, snippet, idle_seconds)."""
    sid = session.get("sessionId", "")
    path = _transcript_for(sid)
    base = {
        "name": session.get("name") or sid[:8],
        "session_id": sid,
        "pid": session.get("pid"),
        "cwd": session.get("cwd", ""),
        "state": "working",
        "snippet": "",
        "idle_seconds": 0,
        "title": "",
        "branch": "",
        "last_prompt": "",
        "last_error": False,
        "model": "",
    }
    if not path:
        return base

    mtime_age = time.time() - os.path.getmtime(path)
    events = _tail_events(path)
    for e in events:  # title / branch / last prompt = what makes the session recognizable
        base["title"] = e.get("aiTitle") or base["title"]
        base["branch"] = e.get("gitBranch") or base["branch"]
        base["last_prompt"] = e.get("lastPrompt") or base["last_prompt"]
    last = _last_assistant(events)

    # State = whose turn it is (last significant event), not write freshness:
    # a long-running tool must not fall back to "working".
    ended = True
    for e in reversed(events):
        if e.get("type") == "assistant":
            stop = (e.get("message") or {}).get("stop_reason")
            ended = stop in ("end_turn", "stop_sequence", "max_tokens")
            break
        if e.get("type") == "user":
            content = (e.get("message") or {}).get("content")
            is_tool_result = "toolUseResult" in e or (
                isinstance(content, list)
                and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
            )
            if not is_tool_result:  # prompt awaiting a reply => Claude is working
                ended = False
                break

    if _pending_question(events):  # question displayed, unanswered -> your turn
        base["state"] = "waiting"
    elif mtime_age < STREAMING_WINDOW:
        base["state"] = "working"
    elif ended:  # user's turn
        base["state"] = "idle" if mtime_age >= WAITING_WINDOW else "waiting"
    elif mtime_age >= STUCK_WINDOW:  # Claude's turn but silent for too long
        base["state"] = "stuck"
    else:
        base["state"] = "working"

    base["last_error"] = _last_error(events)
    base["idle_seconds"] = int(mtime_age)
    if last:
        base["snippet"] = _text_of(last)[:110]
        base["model"] = (last.get("message") or {}).get("model") or ""
    return base


def _demo_sessions():
    """Fake sessions spanning a few projects, one per state, injected when
    CLAUDE_TRAY_FAKE=1. Under a throwaway HOME with seeded transcripts, the
    Search / Insight windows show fake data too (see demo/)."""
    # (state, idle_seconds, title, last_error, model, folder, branch, snippet)
    specs = [
        ("waiting", 40, "Add Stripe checkout flow", False, "claude-sonnet-5",
         "webapp", "feat/checkout",
         "Payment intent wired; need your call on webhook secret handling."),
        ("stuck", 815, "Debug failing DB migration", True, "claude-opus-4-8",
         "api-server", "main",
         "alembic upgrade head raised a duplicate column error"),
        ("stuck", 1560, "Cross-compile armhf release", False, "claude-opus-4-8",
         "dotfiles", "main", "building the release target for armhf"),
        ("working", 3, "Add pagination to /users", False, "claude-sonnet-5",
         "api-server", "fix/rate-limit", "Adding cursor-based pagination"),
        ("idle", 1180, "Write integration tests", False, "claude-haiku-4-5",
         "api-server", "fix/rate-limit", "Happy path covered; edge cases remain"),
        ("idle", 3900, "Tune LR schedule", False, "claude-fable-5",
         "ml-pipeline", "experiment/lr-schedule", "Swept cosine vs step decay"),
    ]
    return [{
        "name": f"{folder}-demo{i}", "session_id": f"demo-{i}", "pid": None,
        "cwd": os.path.join(HOME, "projects", folder), "branch": branch,
        "snippet": snippet, "last_prompt": "",
        "state": state, "idle_seconds": idle, "title": title, "last_error": err,
        "model": model,
    } for i, (state, idle, title, err, model, folder, branch, snippet)
        in enumerate(specs)]


def _demo_usage():
    """Fake subscription quota for CLAUDE_TRAY_FAKE screenshots. Fable at 0 is
    hidden by the menu, same as the real path."""
    now = time.time()
    return [
        {"label": "Current session", "short": "session", "pct": 37,
         "reset_epoch": now + 2 * 3600 + 20 * 60},
        {"label": "Current week (all models)", "short": "week", "pct": 58,
         "reset_epoch": now + 3 * 86400},
        {"label": "Current week (Fable)", "short": "Fable", "pct": 0,
         "reset_epoch": now + 3 * 86400},
    ]


def snapshot():
    # `claude agents` can return the same sessionId twice (agent registration
    # left over after an IDE reconnect) -> dedup, otherwise a duplicate in the menu.
    seen = set()
    sessions = []
    for s in list_live_sessions():
        sid = s.get("sessionId")
        if sid in seen:
            continue
        seen.add(sid)
        sessions.append(session_state(s))
    if os.environ.get("CLAUDE_TRAY_FAKE"):
        sessions += _demo_sessions()
    sessions.sort(key=lambda s: s["idle_seconds"])  # most recently active first
    return sessions


def pretty_folder(cwd):
    return cwd.replace(HOME, "~")


def humanize(seconds):
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}min"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"
    return f"{seconds // 86400}j"


_MODEL_SHORT = {
    "claude-opus-4-8": "opus 4.8",
    "claude-opus-4-7": "opus 4.7",
    "claude-opus-4-6": "opus 4.6",
    "claude-fable-5": "fable 5",
    "claude-sonnet-5": "sonnet 5",
    "claude-sonnet-4-6": "sonnet 4.6",
    "claude-haiku-4-5": "haiku 4.5",
}


def model_label(model):
    """« claude-opus-4-8 » -> « opus 4.8 ». Readable fallback for an unknown id."""
    if not model:
        return ""
    return _MODEL_SHORT.get(model, model.replace("claude-", "").replace("-", " "))


def human_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def session_handle(name, cwd):
    """Short, unique suffix of a session (« f7 » for « myproject-f7 »), the
    handle assigned by `claude agents`. Distinguishes two sessions with an
    identical label (same aiTitle, same folder, same branch)."""
    folder = os.path.basename(cwd.rstrip("/"))
    if folder and name.startswith(folder + "-"):
        return name[len(folder) + 1:]
    return name


_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def reset_clock(epoch):
    """Absolute reset time: « 15h00 » today, « Thu 15h00 » otherwise."""
    dt = datetime.fromtimestamp(epoch)
    hm = f"{dt.hour}h{dt.minute:02d}"
    if dt.date() == datetime.now().date():
        return hm
    return f"{_WEEKDAYS[dt.weekday()]} {hm}"


def _focus_snippet(cwd):
    """Shell snippet that raises the folder's window via the GNOME extension.
    Best-effort: without the extension (`io.github.corentin_core.ClaudeFocus`), fails silently."""
    pattern = os.path.basename(cwd.rstrip("/")) or cwd
    return (
        "gdbus call --session --dest io.github.corentin_core.ClaudeFocus "
        "--object-path /io/github/corentin_core/ClaudeFocus "
        f"--method io.github.corentin_core.ClaudeFocus.FocusWindow {shlex.quote(pattern)} "
        ">/dev/null 2>&1 || true"
    )


def _window_is_focused(cwd):
    """True if the folder's VSCode window has focus (via the GNOME extension).
    Best-effort: without the extension, returns False -> we notify anyway."""
    pattern = os.path.basename(cwd.rstrip("/")) or cwd
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "io.github.corentin_core.ClaudeFocus",
             "--object-path", "/io/github/corentin_core/ClaudeFocus",
             "--method", "io.github.corentin_core.ClaudeFocus.IsFocused", pattern],
            capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL,
        ).stdout
        return "true" in out.lower()
    except Exception:
        return False


def _spawn(script):
    subprocess.Popen(["sh", "-c", script], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_session(session):
    """Raises the right window (GNOME extension) then targets the session by
    ID (deep link): together -> right window + right tab, even cross-window."""
    sid = session.get("session_id")
    cwd = session.get("cwd", "")
    code = shutil.which("code") or "/usr/bin/code"
    if TARGET_SESSION_BY_ID and sid:
        url = f"vscode://Anthropic.claude-code/open?session={sid}"
        target = f"{shlex.quote(code)} --open-url {shlex.quote(url)}"
    else:
        target = f"{shlex.quote(code)} {shlex.quote(cwd)}"
    _spawn(f"{_focus_snippet(cwd)}; sleep {FOCUS_DELAY}; {target}")


def kill_session(pid):
    """SIGTERM to a session's process. True if the signal was sent.
    Clean for a session with no tab; leaves a dead panel on the VSCode side
    if a tab was still displaying it (no API to close a specific tab)."""
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except (OSError, ValueError, TypeError):
        return False


def list_projects():
    """Known projects, inferred from the cwd field of transcripts, sorted by
    recency. Feeds the new-session launcher."""
    seen, projects = set(), []
    dirs = [d for d in glob.glob(os.path.join(PROJECTS, "*")) if os.path.isdir(d)]
    for d in sorted(dirs, key=os.path.getmtime, reverse=True):
        jsonls = glob.glob(os.path.join(d, "*.jsonl"))
        if not jsonls:
            continue
        newest = max(jsonls, key=os.path.getmtime)
        cwd = None
        try:
            with open(newest, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 50:
                        break
                    if not line.lstrip().startswith("{"):
                        continue
                    try:
                        cwd = json.loads(line).get("cwd")
                    except json.JSONDecodeError:
                        continue
                    if cwd:
                        break
        except OSError:
            continue
        if cwd and cwd not in seen and os.path.isdir(cwd):
            seen.add(cwd)
            projects.append(cwd)
    return projects[:12]


def open_new_session(cwd):
    """Opens the folder's window (`code`), raises it (extension), then starts
    a Claude session (deep link without an ID). The sleep lets the window open."""
    code = shutil.which("code") or "/usr/bin/code"
    url = "vscode://Anthropic.claude-code/open"
    _spawn(
        f"{shlex.quote(code)} {shlex.quote(cwd)}; sleep 1.5; "
        f"{_focus_snippet(cwd)}; sleep {FOCUS_DELAY}; {shlex.quote(code)} --open-url {shlex.quote(url)}"
    )


def open_archived_session(cwd, session_id):
    """Opens the folder before targeting the session: resuming a session
    outside its primary folder gives an empty session."""
    code = shutil.which("code") or "/usr/bin/code"
    url = f"vscode://Anthropic.claude-code/open?session={session_id}"
    _spawn(
        f"{shlex.quote(code)} {shlex.quote(cwd)}; sleep 1.5; "
        f"{_focus_snippet(cwd)}; sleep {FOCUS_DELAY}; {shlex.quote(code)} --open-url {shlex.quote(url)}"
    )


# --------------------------------------------------------------------------- #
# Full-text index (SQLite FTS5)
# --------------------------------------------------------------------------- #
# Search on message CONTENT, not just metadata. Incremental index by mtime:
# only modified transcripts get reparsed. One FTS row per user/assistant text
# message, plus one synthetic row per session (title/folder/branch/prompt) so
# a single MATCH also covers metadata. Diacritics-insensitive tokenizer =>
# "amelior" matches "Amélioration".
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
_index_lock = threading.Lock()
_SCHEMA_VERSION = 4  # bump => tables dropped and rebuilt from the transcripts

# Columns of `meta` in the order read by `_row`.
_META_COLS = "path, mtime, session_id, cwd, folder, title, branch, last_prompt"

# Headless `claude -p` runs: indexed for mtime tracking, never searched.
_HEADLESS_ENTRYPOINTS = ("sdk-cli",)
_INTERACTIVE = "entrypoint NOT IN ('" + "','".join(_HEADLESS_ENTRYPOINTS) + "')"


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    if conn.execute("PRAGMA user_version").fetchone()[0] < _SCHEMA_VERSION:
        conn.executescript(
            "DROP TABLE IF EXISTS meta; DROP TABLE IF EXISTS msg;"
            "DROP TABLE IF EXISTS usage; DROP TABLE IF EXISTS usage_turns;"
        )
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(
            path TEXT PRIMARY KEY, mtime REAL, session_id TEXT,
            cwd TEXT, folder TEXT, title TEXT, branch TEXT, last_prompt TEXT,
            entrypoint TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS msg USING fts5(
            body, path UNINDEXED, role UNINDEXED,
            tokenize = 'unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS usage(
            path TEXT, day TEXT, model TEXT,
            input INTEGER, cache_read INTEGER,
            cache_write_5m INTEGER, cache_write_1h INTEGER, output INTEGER,
            PRIMARY KEY (path, day, model));
        CREATE TABLE IF NOT EXISTS usage_turns(
            path TEXT, day TEXT, turns INTEGER, PRIMARY KEY (path, day));
        CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
        """
    )
    return conn


def _message_bodies(path):
    """Text of a transcript's user/assistant messages (`text` blocks only;
    tool_use / tool_result / thinking excluded, subagents excluded)."""
    bodies = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
                    continue
                role = e["type"]
                content = e.get("message", {}).get("content")
                parts = []
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts += [
                        b["text"]
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                    ]
                text = " ".join(p for p in parts if p).strip()
                if text:
                    bodies.append((role, text))
    except OSError:
        pass
    return bodies


def _meta_of(path):
    """Metadata of a transcript, read from the tail (they repeat there)."""
    title = branch = last_prompt = cwd = entrypoint = ""
    for e in _tail_events(path):
        title = e.get("aiTitle") or title
        branch = e.get("gitBranch") or branch
        last_prompt = e.get("lastPrompt") or last_prompt
        cwd = e.get("cwd") or cwd
        entrypoint = e.get("entrypoint") or entrypoint
    return title, branch, last_prompt, cwd, entrypoint


def _local_day(ts):
    """ISO UTC timestamp (« 2026-07-17T13:36:30.576Z ») -> local day « 2026-07-17 »."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def _forget_transcript(conn, path):
    for table in ("meta", "msg", "usage", "usage_turns"):
        conn.execute(f"DELETE FROM {table} WHERE path = ?", (path,))


def _usage_of(path):
    """Usage of a transcript, broken down by (local day, model):
    (usage_rows {(day, model): [input, cache_read, cw5m, cw1h, output]},
     turn_rows {day: number of user prompts}). Turns on Claude's side
     (tool_result) don't count as prompts."""
    usage_rows = {}
    turn_rows = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("isSidechain"):
                    continue
                kind = e.get("type")
                if kind == "assistant":
                    usage = (e.get("message") or {}).get("usage") or {}
                    if not usage:
                        continue
                    day = _local_day(e.get("timestamp"))
                    if not day:
                        continue
                    model = (e.get("message") or {}).get("model") or "?"
                    row = usage_rows.setdefault((day, model), [0, 0, 0, 0, 0])
                    row[0] += usage.get("input_tokens", 0)
                    row[1] += usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation")
                    if isinstance(cc, dict):
                        row[2] += cc.get("ephemeral_5m_input_tokens", 0)
                        row[3] += cc.get("ephemeral_1h_input_tokens", 0)
                    else:  # old format: no TTL breakdown
                        row[2] += usage.get("cache_creation_input_tokens", 0)
                    row[4] += usage.get("output_tokens", 0)
                elif kind == "user":
                    content = (e.get("message") or {}).get("content")
                    is_tool_result = "toolUseResult" in e or (
                        isinstance(content, list)
                        and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
                    )
                    if is_tool_result:
                        continue
                    day = _local_day(e.get("timestamp"))
                    if day:
                        turn_rows[day] = turn_rows.get(day, 0) + 1
    except OSError:
        pass
    return usage_rows, turn_rows


def history_sync():
    """Updates the index: reparses new/modified transcripts, purges the ones
    gone. Returns the number of transcripts (re)indexed."""
    with _index_lock:
        conn = _db()
        try:
            on_disk = {}
            for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
                try:
                    on_disk[path] = os.path.getmtime(path)
                except OSError:
                    continue
            known = dict(conn.execute("SELECT path, mtime FROM meta").fetchall())

            for path in set(known) - set(on_disk):
                _forget_transcript(conn, path)

            reindexed = 0
            for path, mtime in on_disk.items():
                if abs(known.get(path, -1) - mtime) < 1e-6:
                    continue
                _forget_transcript(conn, path)
                session_id = os.path.splitext(os.path.basename(path))[0]
                title, branch, last_prompt, cwd, entrypoint = _meta_of(path)
                folder = os.path.basename(cwd.rstrip("/"))
                conn.execute(
                    "INSERT INTO meta VALUES (?,?,?,?,?,?,?,?,?)",
                    (path, mtime, session_id, cwd, folder, title, branch,
                     last_prompt, entrypoint),
                )
                reindexed += 1
                usage_rows, turn_rows = _usage_of(path)
                conn.executemany(
                    "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
                    [(path, day, model, *tok) for (day, model), tok in usage_rows.items()],
                )
                conn.executemany(
                    "INSERT INTO usage_turns VALUES (?,?,?)",
                    [(path, day, n) for day, n in turn_rows.items()],
                )
                if entrypoint in _HEADLESS_ENTRYPOINTS:
                    continue  # mtime tracking only, no searchable content
                meta_body = " ".join((title, folder, branch, last_prompt))
                conn.execute(
                    "INSERT INTO msg(body, path, role) VALUES (?,?,?)",
                    (meta_body, path, "meta"),
                )
                conn.executemany(
                    "INSERT INTO msg(body, path, role) VALUES (?,?,?)",
                    [(body, path, role) for role, body in _message_bodies(path)],
                )
            conn.commit()
            return reindexed
        finally:
            conn.close()


def _match_query(query):
    """FTS5 prefix query (search-as-you-type) built from the typed text:
    each word becomes `"word"*`, combined with an implicit AND. None if
    there's nothing usable."""
    tokens = re.findall(r"\w+", query, re.UNICODE)
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens)


def _row(meta_row, snippet):
    path, mtime, session_id, cwd, folder, title, branch, last_prompt = meta_row
    return {
        "path": path,
        "mtime": mtime,
        "session_id": session_id,
        "cwd": cwd,
        "folder": folder,
        "title": title,
        "branch": branch,
        "last_prompt": last_prompt,
        "snippet": snippet,
    }


def _filters(folder, branch, since):
    """SQL clauses (folder / branch / since) to concatenate onto a WHERE."""
    clauses, params = [], []
    if folder:
        clauses.append("folder = ?"); params.append(folder)
    if branch:
        clauses.append("branch = ?"); params.append(branch)
    if since:
        clauses.append("mtime >= ?"); params.append(since)
    return "".join(" AND " + c for c in clauses), params


def history_facets():
    """Distinct folders and branches present in the index, sorted."""
    conn = _db()
    try:
        folders = [r[0] for r in conn.execute(
            f"SELECT DISTINCT folder FROM meta WHERE folder != '' AND {_INTERACTIVE} "
            "ORDER BY folder")]
        branches = [r[0] for r in conn.execute(
            f"SELECT DISTINCT branch FROM meta WHERE branch != '' AND {_INTERACTIVE} "
            "ORDER BY branch")]
        return folders, branches
    finally:
        conn.close()


def history_recent(limit=300, folder="", branch="", since=0.0):
    """Recent sessions (empty query), filtered, sorted by recency."""
    clause, params = _filters(folder, branch, since)
    conn = _db()
    try:
        rows = conn.execute(
            f"SELECT {_META_COLS} FROM meta WHERE {_INTERACTIVE}{clause} "
            "ORDER BY mtime DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [_row(r, "") for r in rows]
    finally:
        conn.close()


def history_search(query, limit=300, folder="", branch="", since=0.0):
    """Full-text search, one entry per transcript (best passage), sorted by
    relevance (bm25). Folder / branch / since filters applied before the
    limit."""
    match = _match_query(query)
    if match is None:
        return history_recent(limit, folder, branch, since)
    clause, params = _filters(folder, branch, since)
    conn = _db()
    try:
        hits = conn.execute(
            "SELECT path, role, snippet(msg, 0, '«', '»', '…', 12) AS snip "
            "FROM msg WHERE msg MATCH ? ORDER BY rank",
            (match,),
        ).fetchall()
        results, seen = [], set()
        for path, role, snip in hits:
            if path in seen:
                continue
            seen.add(path)
            meta_row = conn.execute(
                f"SELECT {_META_COLS} FROM meta WHERE path = ?{clause}",
                [path] + params,
            ).fetchone()
            if meta_row:
                # Match on metadata: the excerpt would repeat the columns,
                # show the last prompt instead.
                snippet = meta_row[7][:120] if role == "meta" else snip
                results.append(_row(meta_row, snippet))
            if len(results) >= limit:
                break
        return results
    except sqlite3.OperationalError:
        return []  # invalid FTS syntax while typing
    finally:
        conn.close()


def usage_report(day=None):
    """Usage for a local day (default: today) ->
    (per_model, per_folder, turns, conversations).
    per_model: [(model, input, cache_read, cw5m, cw1h, output)] sorted by output.
    per_folder: [(folder, model, ...same columns)] to aggregate cost per folder."""
    day = day or datetime.now().date().isoformat()
    conn = _db()
    try:
        per_model = conn.execute(
            "SELECT model, SUM(input), SUM(cache_read), SUM(cache_write_5m), "
            "SUM(cache_write_1h), SUM(output) FROM usage WHERE day = ? "
            "GROUP BY model ORDER BY 6 DESC", (day,)).fetchall()
        per_folder = conn.execute(
            "SELECT COALESCE(m.folder, '?'), u.model, SUM(u.input), SUM(u.cache_read), "
            "SUM(u.cache_write_5m), SUM(u.cache_write_1h), SUM(u.output) "
            "FROM usage u LEFT JOIN meta m ON m.path = u.path WHERE u.day = ? "
            "GROUP BY m.folder, u.model", (day,)).fetchall()
        turns = conn.execute(
            "SELECT SUM(turns) FROM usage_turns WHERE day = ?", (day,)).fetchone()[0] or 0
        convs = conn.execute(
            "SELECT COUNT(DISTINCT path) FROM usage WHERE day = ?", (day,)).fetchone()[0] or 0
        return per_model, per_folder, turns, convs
    finally:
        conn.close()


def usage_daily_tokens(days=7):
    """Tokens and number of turns per day, most recent to oldest, last N days."""
    conn = _db()
    try:
        tok_rows = conn.execute(
            "SELECT day, SUM(input + cache_read + cache_write_5m + cache_write_1h + output) "
            "FROM usage GROUP BY day").fetchall()
        turn_rows = dict(conn.execute("SELECT day, SUM(turns) FROM usage_turns GROUP BY day"))
    finally:
        conn.close()
    ordered = sorted(tok_rows, reverse=True)[:days]
    return [(day, toks, turn_rows.get(day, 0)) for day, toks in ordered]


# --------------------------------------------------------------------------- #
# Headless debug mode
# --------------------------------------------------------------------------- #
if "--once" in sys.argv:
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
    sys.exit(0)

if "--usage" in sys.argv:
    print(json.dumps(fetch_usage(), indent=2, ensure_ascii=False))
    sys.exit(0)

if "--reindex" in sys.argv:
    t0 = time.time()
    n = history_sync()
    print(f"{n} transcript(s) (re)indexed in {time.time() - t0:.2f}s → {DB_PATH}")
    sys.exit(0)

if "--insight" in sys.argv:
    history_sync()
    per_model, _per_folder, turns, convs = usage_report()
    tokens = sum(sum(cols) for _m, *cols in per_model)
    print(f"Today: {human_tokens(tokens)} tokens · "
          f"{turns} turns · {convs} conversations")
    for model, *cols in per_model:
        print(f"  {model_label(model) or model:<12} {human_tokens(sum(cols)):>7}  "
              f"(output {human_tokens(cols[4])})")
    print("Last 7 days:")
    for day, toks, day_turns in usage_daily_tokens():
        print(f"  {day}  {human_tokens(toks):>7}  {day_turns} turns")
    sys.exit(0)

if "--search" in sys.argv:
    history_sync()
    q = " ".join(a for a in sys.argv[sys.argv.index("--search") + 1:] if not a.startswith("--"))
    for e in history_search(q, limit=25):
        folder = os.path.basename(e["cwd"].rstrip("/")) or "?"
        print(f"[{folder}] {e['title'] or e['session_id'][:8]}\n    {e['snippet']}")
    sys.exit(0)


# --------------------------------------------------------------------------- #
# GTK / AppIndicator applet
# --------------------------------------------------------------------------- #
import gi  # type: ignore[import-not-found]

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango, AyatanaAppIndicator3 as AppIndicator  # type: ignore  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # type: ignore
    Notify.init("claude-sessions")
except (ValueError, ImportError):
    Notify = None  # type: ignore

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "claude.png")

# DBus service called by the GNOME extension when the global shortcut is pressed.
DBUS_NAME = "io.github.corentin_core.ClaudeTray"
DBUS_PATH = "/io/github/corentin_core/ClaudeTray"
DBUS_IFACE_XML = """
<node>
  <interface name="io.github.corentin_core.ClaudeTray">
    <method name="OpenSearch"/>
  </interface>
</node>
"""


class SearchWindow(Gtk.Window):
    """Full-text search across the conversation history: the query matches
    message content as well as metadata (title, folder, branch, prompt), via
    the FTS5 index. Focus stays in the field: ↑/↓ move the selection, Enter
    opens the selected row (double-click too)."""

    # ListStore: 5 displayed columns, then cwd/id (for opening) and mtime
    # (sorts "When" on a numeric value while the column displays text).
    COL_CWD, COL_SID, COL_MTIME = 5, 6, 7

    # Date filter presets: (label, max age in seconds; 0 = all).
    _DATE_PRESETS = (
        ("Always", 0), ("24 h", 86400), ("7 days", 604800),
        ("30 days", 2592000), ("3 months", 7776000),
    )

    def __init__(self):
        super().__init__(title="Search a conversation")
        self.set_default_size(920, 540)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("key-press-event", self._on_key)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        self.add(box)

        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Message content, title, folder, branch…")
        self.entry.connect("search-changed", self._on_search)
        self.entry.connect("activate", self._open_selected)
        box.pack_start(self.entry, False, False, 0)

        history_sync()  # incremental: near-instant once the index is warm
        box.pack_start(self._make_filter_bar(), False, False, 0)

        self.count = Gtk.Label(xalign=0)
        self.count.get_style_context().add_class("dim-label")
        box.pack_start(self.count, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str, str, str, float)
        self.view = Gtk.TreeView(model=self.store)
        self.view.connect("row-activated", self._on_row_activated)
        headers = ("Conversation", "Folder", "Branch", "When", "Excerpt")
        sort_cols = (0, 1, 2, self.COL_MTIME, -1)  # "When" sorts on mtime
        fixed_widths = {1: 110, 2: 120}  # folder / branch: compact
        min_widths = {0: 280, 4: 200}  # conversation dominates, excerpt follows
        for i, header in enumerate(headers):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(header, renderer, text=i)
            col.set_resizable(True)
            if sort_cols[i] >= 0:
                col.set_sort_column_id(sort_cols[i])
            if i in fixed_widths:
                col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
                col.set_fixed_width(fixed_widths[i])
            if i in (0, 4):  # title and excerpt absorb the space
                renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
                col.set_expand(True)
                col.set_min_width(min_widths[i])
            self.view.append_column(col)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        box.pack_start(scroller, True, True, 0)

        self._search()
        self.entry.grab_focus()

    def _make_filter_bar(self):
        """Folder / branch / since bar. Each combo re-triggers the search.
        Populated from the index facets (already synced)."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        folders, branches = history_facets()

        self.folder_combo = Gtk.ComboBoxText()
        self.folder_combo.append("", "All folders")
        for f in folders:
            self.folder_combo.append(f, f)
        self.branch_combo = Gtk.ComboBoxText()
        self.branch_combo.append("", "All branches")
        for b in branches:
            self.branch_combo.append(b, b)
        self.date_combo = Gtk.ComboBoxText()
        for label, secs in self._DATE_PRESETS:
            self.date_combo.append(str(secs), label)
        for combo in (self.folder_combo, self.branch_combo, self.date_combo):
            combo.set_active(0)
            combo.connect("changed", self._on_search)
        bar.pack_start(self.folder_combo, False, False, 0)
        bar.pack_start(self.branch_combo, False, False, 0)
        bar.pack_start(Gtk.Label(label="Since"), False, False, 0)
        bar.pack_start(self.date_combo, False, False, 0)
        return bar

    def _search(self):
        now = time.time()
        query = self.entry.get_text()
        secs = int(self.date_combo.get_active_id() or 0)
        results = history_search(
            query,
            folder=self.folder_combo.get_active_id() or "",
            branch=self.branch_combo.get_active_id() or "",
            since=now - secs if secs else 0,
        )
        self.store.clear()
        for e in results:
            title = e["title"] or e["last_prompt"][:70] or e["session_id"][:8]
            when = humanize(int(now - e["mtime"]))
            self.store.append(
                [title, e["folder"] or "?", e["branch"], when, e["snippet"],
                 e["cwd"], e["session_id"], e["mtime"]]
            )
        label = f"{len(results)} conversation(s)"
        self.count.set_text(label if query.strip() else f"{label} · recent")
        self._select_first()

    def _on_search(self, _widget):
        self._search()

    def _open(self, it):
        cwd = self.store[it][self.COL_CWD]
        if cwd:
            open_archived_session(cwd, self.store[it][self.COL_SID])
            self.destroy()

    def _open_selected(self, _entry):
        _m, it = self.view.get_selection().get_selected()
        if it is None:
            it = self.store.get_iter_first()
        if it is not None:
            self._open(it)

    def _on_row_activated(self, _view, path, _col):
        self._open(self.store.get_iter(path))

    def _select_first(self):
        sel = self.view.get_selection()
        it = self.store.get_iter_first()
        if it is None:
            sel.unselect_all()
        else:
            sel.select_iter(it)

    def _move(self, delta):
        """Moves the selection without leaving the search field."""
        n = self.store.iter_n_children(None)
        if n == 0:
            return
        sel = self.view.get_selection()
        _m, it = sel.get_selected()
        idx = 0 if it is None else self.store.get_path(it).get_indices()[0] + delta
        idx = max(0, min(idx, n - 1))
        target = self.store.iter_nth_child(None, idx)
        if target is not None:
            sel.select_iter(target)
            self.view.scroll_to_cell(self.store.get_path(target), None, False, 0, 0)

    def _on_key(self, _widget, event):
        kv = event.keyval
        if kv == Gdk.KEY_Escape:
            self.destroy()
            return True
        if kv in (Gdk.KEY_Down, Gdk.KEY_Page_Down):
            self._move(10 if kv == Gdk.KEY_Page_Down else 1)
            return True
        if kv in (Gdk.KEY_Up, Gdk.KEY_Page_Up):
            self._move(-10 if kv == Gdk.KEY_Page_Up else -1)
            return True
        return False


class SessionsWindow(Gtk.Window):
    """Live sessions, one row each: click the row to open, the ✕ button to
    close (SIGTERM). A ✕ per row needs real widgets, hence a window: the
    tray menu (dbusmenu) only renders an abstract model."""

    MARKS = {"working": "🔵", "waiting": "🟠", "idle": "⚪", "stuck": "🔴"}

    def __init__(self):
        super().__init__(title="Live Claude sessions")
        self.set_default_size(720, 460)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("key-press-event", self._on_key)
        self._rows = {}  # ListBoxRow -> session

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        self.add(box)

        self.count = Gtk.Label(xalign=0)
        self.count.get_style_context().add_class("dim-label")
        box.pack_start(self.count, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect("row-activated", self._on_row_activated)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.listbox)
        box.pack_start(scroller, True, True, 0)

        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self._reload())
        box.pack_start(refresh, False, False, 0)

        self._reload()

    def _reload(self):
        for row in list(self._rows):
            self.listbox.remove(row)
        self._rows.clear()
        for s in snapshot():
            self._rows[self._make_row(s)] = s
        for row in self._rows:
            self.listbox.add(row)
        self._update_count()
        self.listbox.show_all()

    def _make_row(self, s):
        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_border_width(4)
        mark = self.MARKS.get(s["state"], "⚪")
        title = s["title"] or s["last_prompt"][:60] or s["name"]
        text = f"{mark}  {title}"
        folder = os.path.basename(s["cwd"].rstrip("/"))
        if folder:
            text += f"   [{folder}]"
        if s["branch"]:
            text += f"   [{s['branch']}]"
        if s.get("model"):
            text += f"   · {model_label(s['model'])}"
        text += f"   · {humanize(s['idle_seconds'])}"
        label = Gtk.Label(label=text, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        hbox.pack_start(label, True, True, 0)

        close = Gtk.Button(label="✕")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.get_style_context().add_class("destructive-action")
        if s.get("pid"):
            close.set_tooltip_text("Close the session (SIGTERM)")
            close.connect("clicked", lambda _b, r=row: self._close_row(r))
        else:
            close.set_tooltip_text("unknown pid (demo)")
            close.set_sensitive(False)
        hbox.pack_end(close, False, False, 0)

        row.add(hbox)
        return row

    def _on_row_activated(self, _lb, row):
        s = self._rows.get(row)
        if s:
            open_session(s)
            self.destroy()

    def _close_row(self, row):
        s = self._rows.get(row)
        if s and kill_session(s.get("pid")):
            self._rows.pop(row, None)
            self.listbox.remove(row)
            self._update_count()

    def _update_count(self):
        self.count.set_text(f"{len(self._rows)} live session(s)")

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False


class InsightWindow(Gtk.Window):
    """Today's token usage: total, breakdown by model and by folder, plus the
    last 7 days. The real budget is the subscription quota, shown at the top
    (no $ cost: the subscription isn't billed per token)."""

    def __init__(self, usage=None):
        super().__init__(title="Today's insight")
        self.set_default_size(720, 620)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("key-press-event", self._on_key)
        self._usage = usage or []

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.box.set_border_width(8)
        self.add(self.box)
        self._reload()

    def _reload(self):
        for child in self.box.get_children():
            self.box.remove(child)
        history_sync()  # incremental: near-instant once the index is warm
        per_model, per_folder, turns, convs = usage_report()

        tokens = sum(sum(cols) for _m, *cols in per_model)
        head = Gtk.Label(xalign=0)
        head.set_markup(
            f"<big><b>Today — {human_tokens(tokens)} tokens</b></big>   "
            f"{turns} turns · {convs} conversations")
        self.box.pack_start(head, False, False, 0)
        quota = self._quota_line()
        if quota:
            sub = Gtk.Label(xalign=0)
            sub.get_style_context().add_class("dim-label")
            sub.set_text(quota)
            self.box.pack_start(sub, False, False, 0)

        model_rows = [
            [model_label(m) or m, human_tokens(sum(cols)),
             human_tokens(cols[0]), human_tokens(cols[1]),
             human_tokens(cols[2] + cols[3]), human_tokens(cols[4])]
            for m, *cols in sorted(per_model, key=lambda r: -sum(r[1:]))
        ]
        self._add_section("By model",
                           ["Model", "Total", "Input", "Cache read", "Cache write", "Output"],
                           model_rows)

        folders = {}  # folder -> tokens
        for folder, _model, *cols in per_folder:
            folders[folder] = folders.get(folder, 0) + sum(cols)
        folder_rows = [
            [folder, human_tokens(toks)]
            for folder, toks in sorted(folders.items(), key=lambda kv: -kv[1])
        ]
        self._add_section("By folder", ["Folder", "Tokens"], folder_rows)

        day_rows = [[day, human_tokens(toks), f"{day_turns} turns"]
                    for day, toks, day_turns in usage_daily_tokens()]
        self._add_section("Last 7 days", ["Day", "Tokens", "Turns"], day_rows)

        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self._reload())
        self.box.pack_start(refresh, False, False, 0)
        self.box.show_all()

    def _add_section(self, title, headers, rows):
        label = Gtk.Label(xalign=0)
        label.set_markup(f"<b>{title}</b>")
        label.set_margin_top(8)
        self.box.pack_start(label, False, False, 0)
        store = Gtk.ListStore(*([str] * len(headers)))
        for row in rows:
            store.append(row)
        view = Gtk.TreeView(model=store)
        view.set_can_focus(False)
        for i, header in enumerate(headers):
            renderer = Gtk.CellRendererText()
            if i > 0:  # numeric columns right-aligned
                renderer.set_property("xalign", 1.0)
            col = Gtk.TreeViewColumn(header, renderer, text=i)
            col.set_expand(i == 0)
            view.append_column(col)
        self.box.pack_start(view, False, False, 0)

    def _quota_line(self):
        """« Quota: session 16% (→13h19) · week 21% (→14h59) » (Fable hidden if 0%).
        The quota is the real subscription ceiling — it's the budget signal."""
        cells = []
        for u in self._usage:
            if u["short"] == "Fable" and u["pct"] == 0:
                continue
            cell = f"{u['short']} {u['pct']}%"
            if u["reset_epoch"]:
                cell += f" (→{reset_clock(u['reset_epoch'])})"
            cells.append(cell)
        return "Quota: " + "  ·  ".join(cells) if cells else ""

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False


class DBusService:
    """Exposes `OpenSearch` on the session bus. The GNOME extension calls it
    from the global shortcut; the callback arrives on the GTK loop, so we
    open the window directly."""

    def __init__(self, tray):
        self._tray = tray
        self._reg_id = 0
        self._iface = Gio.DBusNodeInfo.new_for_xml(DBUS_IFACE_XML).interfaces[0]
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION, DBUS_NAME, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, None, None)

    def _on_bus_acquired(self, conn, _name):
        self._reg_id = conn.register_object(
            DBUS_PATH, self._iface, self._on_call, None, None)

    def _on_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "OpenSearch":
            self._tray.open_search()
        invocation.return_value(None)


class Tray:
    def __init__(self):
        icon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
        self.ind = AppIndicator.Indicator.new_with_path(
            "claude-sessions",
            "claude",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
            icon_dir,
        )
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.ind.set_menu(self.menu)
        self._sessions = []
        self._usage = []
        self._last_sig = None
        self._search_win = None
        self._sessions_win = None
        self._insight_win = None
        self._prev_states = {}  # session_id -> last state seen, for transitions
        self._notifs = {}  # session_id -> live Notification (ref kept, else GC'd)
        self._dbus = DBusService(self)  # global shortcut -> OpenSearch
        self.refresh()
        # Builds the full-text index in the background: the first search
        # window opened is then already warm (otherwise ~1-2 s of parsing
        # on first run).
        threading.Thread(target=history_sync, daemon=True).start()
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)
        self._poll_usage()
        GLib.timeout_add_seconds(USAGE_POLL_SECONDS, self._poll_usage)

    def _signature(self):
        # what justifies a rebuild; idle time is excluded
        # (it changes every tick and would make the menu jump under the cursor).
        return tuple(
            (s["session_id"], s["state"], s["branch"], s["title"], s["name"])
            for s in self._sessions
        )

    def _tick(self):
        self._sessions = snapshot()
        self._notify_transitions()
        self._refresh_icon()
        sig = self._signature()
        if sig != self._last_sig:  # rebuild only on a real change
            self._last_sig = sig
            self._refresh_menu()
        return True

    def refresh(self):
        self._sessions = snapshot()
        # Seed without notifying: neither startup nor a manual refresh should
        # trigger a burst of notifications for sessions already ready.
        self._prev_states = {s["session_id"]: s["state"] for s in self._sessions}
        self._last_sig = self._signature()
        self._refresh_icon()
        self._refresh_menu()

    def _notify_transitions(self):
        """Notifies each session that just turned 🟠 ready."""
        for s in self._sessions:
            prev = self._prev_states.get(s["session_id"])
            if s["state"] == "waiting" and prev is not None and prev != "waiting":
                self._notify_ready(s)
        self._prev_states = {s["session_id"]: s["state"] for s in self._sessions}

    def _notify_ready(self, s):
        if Notify is None or not NOTIFY_ON_READY:
            return
        if _window_is_focused(s["cwd"]):  # already in view
            return
        sid = s["session_id"]
        folder = os.path.basename(s["cwd"].rstrip("/"))
        summary = s["title"] or s["last_prompt"][:60] or s["name"]
        body_lines = [f"{folder}" + (f"   [{s['branch']}]" if s["branch"] else "")]
        if s["snippet"]:
            body_lines.append(s["snippet"])
        n = Notify.Notification.new(summary, "\n".join(body_lines), ICON_PATH)
        n.add_action("open", "Open", lambda *_a, sess=s: open_session(sess))
        n.add_action("default", "Open", lambda *_a, sess=s: open_session(sess))
        n.connect("closed", lambda _n, i=sid: self._notifs.pop(i, None))
        self._notifs[sid] = n  # keep the ref: without it, the callback doesn't fire
        try:
            n.show()
        except Exception as exc:
            print(f"[tray] notify failed: {exc!r}", file=sys.stderr)

    def _poll_usage(self):
        threading.Thread(target=self._usage_worker, daemon=True).start()
        return True

    def _usage_worker(self):
        usage = fetch_usage()
        if not usage and os.environ.get("CLAUDE_TRAY_FAKE"):
            usage = _demo_usage()
        if usage is not None:
            GLib.idle_add(self._on_usage, usage)

    def _on_usage(self, usage):
        self._usage = usage
        self._refresh_icon()
        self._refresh_menu()
        return False

    def _session_pct(self):
        for u in self._usage:
            if u["short"] == "session":
                return u["pct"]
        return None

    def _refresh_icon(self):
        waiting = sum(1 for s in self._sessions if s["state"] == "waiting")
        stuck = sum(1 for s in self._sessions if s["state"] == "stuck")
        parts = []
        if waiting:
            parts.append(str(waiting))
        if stuck:
            parts.append(f"⚠{stuck}")
        pct = self._session_pct()
        if pct is not None:
            parts.append(f"{pct}%")
        self.ind.set_icon_full("claude", "Claude sessions")
        self.ind.set_label(" " + " · ".join(parts) if parts else "", "00")

    def _refresh_menu(self):
        sessions = self._sessions
        waiting = [s for s in sessions if s["state"] == "waiting"]
        stuck = [s for s in sessions if s["state"] == "stuck"]

        for child in self.menu.get_children():
            self.menu.remove(child)

        if self._usage:
            self._add_disabled("Subscription quota")
            cells = []
            for u in self._usage:
                if u["short"] == "Fable" and u["pct"] == 0:
                    continue
                cell = f"{u['short']} {u['pct']}%"
                if u["reset_epoch"]:
                    cell += f" (→{reset_clock(u['reset_epoch'])})"
                cells.append(cell)
            self._add_disabled("   " + "  ·  ".join(cells))
            self.menu.append(Gtk.SeparatorMenuItem())

        if not sessions:
            self._add_disabled("No Claude sessions")
        else:
            summary = f"{len(waiting)} waiting"
            if stuck:
                summary += f" · {len(stuck)} stuck"
            summary += f" · {len(sessions)} total"
            self._add_disabled(summary)
            self.menu.append(Gtk.SeparatorMenuItem())
            keys = [(self._display_title(s), os.path.basename(s["cwd"].rstrip("/")), s["branch"])
                    for s in sessions]
            collisions = {k for k, n in Counter(keys).items() if n > 1}
            ambiguous = {s["session_id"]: k in collisions for s, k in zip(sessions, keys)}
            attention = [s for s in sessions if s["state"] in ("waiting", "stuck")]
            calm = [s for s in sessions if s["state"] in ("working", "idle")]
            for s in attention:
                self._add_session(s, ambiguous=ambiguous[s["session_id"]])
            if attention and calm:
                self.menu.append(Gtk.SeparatorMenuItem())
            for cwd, group in self._group_by_project(calm):
                if len(group) >= PROJECT_SUBMENU_MIN:
                    self._add_project_submenu(cwd, group, ambiguous)
                else:
                    for s in group:
                        self._add_session(s, ambiguous=ambiguous[s["session_id"]])

        self.menu.append(Gtk.SeparatorMenuItem())
        self._add_new_session_submenu()
        self.menu.append(Gtk.SeparatorMenuItem())
        self._add_action("🔍 Search a conversation…  (Super+K)", lambda _w: self.open_search())
        self._add_action("🧹 Live sessions…  (open / close)", lambda _w: self.open_sessions())
        self._add_action("📊 Today's insight…  (tokens)", lambda _w: self.open_insight())
        self._add_action("Refresh", lambda _w: self.refresh())
        self._add_action("Quit", lambda _w: Gtk.main_quit())
        self.menu.show_all()

    def _add_new_session_submenu(self):
        parent = Gtk.MenuItem(label="➕ New session")
        sub = Gtk.Menu()
        projects = list_projects()
        if not projects:
            empty = Gtk.MenuItem(label="(no known project)")
            empty.set_sensitive(False)
            sub.append(empty)
        else:
            for cwd in projects:
                it = Gtk.MenuItem(label=pretty_folder(cwd))
                it.connect("activate", lambda _w, c=cwd: open_new_session(c))
                sub.append(it)
        parent.set_submenu(sub)
        self.menu.append(parent)

    @staticmethod
    def _display_title(s):
        return s["title"] or s["last_prompt"][:45] or s["name"]

    @staticmethod
    def _group_by_project(sessions):
        """[(cwd, [sessions])] by folder, in recency order: the first group
        carries the most recent session. Assumes `sessions` is sorted by
        increasing age."""
        order, groups = [], {}
        for s in sessions:
            cwd = s["cwd"]
            if cwd not in groups:
                groups[cwd] = []
                order.append(cwd)
            groups[cwd].append(s)
        return [(cwd, groups[cwd]) for cwd in order]

    def _add_project_submenu(self, cwd, group, ambiguous):
        folder = os.path.basename(cwd.rstrip("/")) or "?"
        working = sum(1 for s in group if s["state"] == "working")
        label = f"📁 {folder} ({len(group)})"
        if working:
            label += f"   🔵{working}"
        parent = Gtk.MenuItem(label=label)
        sub = Gtk.Menu()
        for s in group:  # folder redundant in its own submenu
            self._add_session(s, ambiguous=ambiguous[s["session_id"]], menu=sub,
                              show_folder=False)
        parent.set_submenu(sub)
        self.menu.append(parent)

    def _add_session(self, s, ambiguous=False, menu=None, show_folder=True):
        menu = menu if menu is not None else self.menu
        mark = {"working": "🔵", "waiting": "🟠", "idle": "⚪", "stuck": "🔴"}.get(s["state"], "⚪")
        title = self._display_title(s)
        if ambiguous:  # same title + folder + branch as another: disambiguate
            title += f" #{session_handle(s['name'], s['cwd'])}"
        label = f"{mark}  {title}"
        folder = os.path.basename(s["cwd"].rstrip("/"))
        if folder and show_folder:
            label += f"   [{folder}]"
        if s["branch"]:
            label += f"   [{s['branch']}]"
        label += f"   · {humanize(s['idle_seconds'])}"
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda _w, sess=s: open_session(sess))
        tip = pretty_folder(s["cwd"])
        if s["state"] == "stuck":
            why = "stuck on an error" if s["last_error"] else "silent"
            tip += f"\n\n⚠ Claude's turn but {why} for {humanize(s['idle_seconds'])} — possibly stuck."
        if s["snippet"]:
            tip += "\n\n" + s["snippet"]
        item.set_tooltip_text(tip)
        menu.append(item)

    def _add_disabled(self, text):
        item = Gtk.MenuItem(label=text)
        item.set_sensitive(False)
        self.menu.append(item)

    def _add_action(self, text, cb):
        item = Gtk.MenuItem(label=text)
        item.connect("activate", cb)
        self.menu.append(item)

    def open_search(self):
        if self._search_win is not None:
            self._search_win.present()
            return
        self._search_win = SearchWindow()
        self._search_win.connect("destroy", self._on_search_closed)
        self._search_win.show_all()

    def _on_search_closed(self, _win):
        self._search_win = None

    def open_sessions(self):
        if self._sessions_win is not None:
            self._sessions_win.present()
            return
        self._sessions_win = SessionsWindow()
        self._sessions_win.connect("destroy", self._on_sessions_closed)
        self._sessions_win.show_all()

    def _on_sessions_closed(self, _win):
        self._sessions_win = None

    def open_insight(self):
        if self._insight_win is not None:
            self._insight_win.present()
            return
        self._insight_win = InsightWindow(usage=self._usage)
        self._insight_win.connect("destroy", self._on_insight_closed)
        self._insight_win.show_all()

    def _on_insight_closed(self, _win):
        self._insight_win = None


if __name__ == "__main__":
    tray = Tray()
    if "--demo-notify" in sys.argv:  # fire one sample notification for a screenshot
        _demo = next((s for s in _demo_sessions() if s["state"] == "waiting"), None)
        if _demo:
            GLib.timeout_add_seconds(2, lambda: tray._notify_ready(_demo) or False)
    Gtk.main()
