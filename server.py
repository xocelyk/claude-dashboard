#!/usr/bin/env python3
"""Claude Code session dashboard server."""

import json
import os
import subprocess
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PORT = 7227

# Approximate USD-per-token rates by model family. Selection is by substring
# match on the assistant turn's model id (e.g. "claude-opus-4-7" -> opus-4-7).
# Order matters — more-specific keys must come before broader ones; _rates_for
# iterates in insertion order and returns the first hit.
# Rates from https://platform.claude.com/docs/en/about-claude/pricing
MODEL_RATES = {
    # Opus 4.5+ uses the new lower pricing tier
    "opus-4-7": {"input": 5.0 / 1e6,  "output": 25.0 / 1e6, "cache_read": 0.5 / 1e6, "cache_write": 6.25 / 1e6},
    "opus-4-6": {"input": 5.0 / 1e6,  "output": 25.0 / 1e6, "cache_read": 0.5 / 1e6, "cache_write": 6.25 / 1e6},
    "opus-4-5": {"input": 5.0 / 1e6,  "output": 25.0 / 1e6, "cache_read": 0.5 / 1e6, "cache_write": 6.25 / 1e6},
    # Legacy Opus 4.0 / 4.1 pricing
    "opus":     {"input": 15.0 / 1e6, "output": 75.0 / 1e6, "cache_read": 1.5 / 1e6, "cache_write": 18.75 / 1e6},
    "sonnet":   {"input": 3.0 / 1e6,  "output": 15.0 / 1e6, "cache_read": 0.3 / 1e6, "cache_write": 3.75 / 1e6},
    "haiku":    {"input": 1.0 / 1e6,  "output": 5.0 / 1e6,  "cache_read": 0.1 / 1e6, "cache_write": 1.25 / 1e6},
}
DEFAULT_RATES = MODEL_RATES["sonnet"]


def _rates_for(model: str) -> dict:
    m = (model or "").lower()
    for key, rates in MODEL_RATES.items():
        if key in m:
            return rates
    return DEFAULT_RATES


def _estimate_cost(usage: dict, model: str) -> float:
    # Anthropic's usage object reports input_tokens, cache_read_input_tokens,
    # and cache_creation_input_tokens as disjoint counts — input_tokens already
    # excludes anything served from cache or newly written to cache.
    rates = _rates_for(model)
    return (
        usage.get("input_tokens", 0) * rates["input"]
        + usage.get("output_tokens", 0) * rates["output"]
        + usage.get("cache_read_input_tokens", 0) * rates["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * rates["cache_write"]
    )


def _iso_to_epoch(ts_str: str) -> float:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0


# path -> (mtime, parsed_result). parsed_result is None for sdk-cli sub-sessions
# or files that failed to parse / had no real messages.
_PARSE_CACHE: dict[Path, tuple[float, dict | None]] = {}


def _parse_jsonl(path: Path) -> dict | None:
    """Single-pass parse: extracts stats, ai-title, and detects sdk-cli sub-sessions."""
    ai_title = ""
    total = 0
    user_msgs = 0
    assistant_msgs = 0
    first_ts: float | None = None
    last_ts: float | None = None
    first_assistant_ts: float | None = None
    last_assistant_ts: float | None = None
    total_cost = 0.0
    model = ""
    cwd = ""
    sdk_check_done = False

    try:
        with open(path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type", "")

                # First user record decides whether this is a real session or
                # an sdk-cli sub-session (e.g. auto-rename helper).
                if msg_type == "user" and not sdk_check_done:
                    sdk_check_done = True
                    if msg.get("entrypoint") == "sdk-cli":
                        return None

                if msg_type == "ai-title":
                    title = msg.get("aiTitle") or ""
                    if title:
                        ai_title = title
                    continue

                ts = _iso_to_epoch(msg.get("timestamp", "")) if msg.get("timestamp") else 0

                if msg_type in ("user", "assistant"):
                    total += 1
                    if ts:
                        if first_ts is None or ts < first_ts:
                            first_ts = ts
                        if last_ts is None or ts > last_ts:
                            last_ts = ts
                    if not cwd and msg.get("cwd"):
                        cwd = msg["cwd"]

                if msg_type == "user":
                    user_msgs += 1
                elif msg_type == "assistant":
                    assistant_msgs += 1
                    inner = msg.get("message", {})
                    if isinstance(inner, dict):
                        turn_model = inner.get("model") or model
                        usage = inner.get("usage", {})
                        if usage:
                            total_cost += _estimate_cost(usage, turn_model)
                        if inner.get("model"):
                            model = inner["model"]
                        if ts:
                            if first_assistant_ts is None or ts < first_assistant_ts:
                                first_assistant_ts = ts
                            if last_assistant_ts is None or ts > last_assistant_ts:
                                last_assistant_ts = ts
    except OSError:
        return None

    if total == 0:
        return None

    duration_ms = 0
    if first_assistant_ts and last_assistant_ts:
        duration_ms = int((last_assistant_ts - first_assistant_ts) * 1000)

    return {
        "ai_title": ai_title,
        "total_messages": total,
        "user_messages": user_msgs,
        "assistant_messages": assistant_msgs,
        "first_message_at": first_ts,
        "last_message_at": last_ts,
        "total_cost": round(total_cost, 6),
        "total_duration_ms": duration_ms,
        "model": model,
        "cwd": cwd,
    }


def _parse_with_cache(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _PARSE_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    result = _parse_jsonl(path)
    _PARSE_CACHE[path] = (mtime, result)
    return result


def get_jsonl_stats() -> dict[str, dict]:
    stats: dict[str, dict] = {}
    if not PROJECTS_DIR.exists():
        return stats
    seen: set[Path] = set()
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        if "subagents" in str(path):
            continue
        seen.add(path)
        result = _parse_with_cache(path)
        if result is None:
            continue
        stats[path.stem] = result
    # Evict cache entries for files that no longer exist
    for p in list(_PARSE_CACHE.keys()):
        if p not in seen:
            del _PARSE_CACHE[p]
    return stats


def _start_seconds(s: str) -> int | None:
    """Extract the seconds-of-minute from a date string like 'Wed May 13 21:51:38 2026'.

    Timezone-invariant — offsets shift hours/minutes but never seconds, so this
    works regardless of whether the two strings being compared are in different TZs.
    """
    for part in (s or "").split():
        if part.count(":") == 2:
            try:
                return int(part.split(":")[2])
            except ValueError:
                return None
    return None


def is_pid_alive(pid: int, proc_start: str = "") -> bool:
    """True if pid exists and (when proc_start given) its start time matches.

    The proc_start cross-check guards against PID reuse: a recycled PID would
    have a different start time than the one recorded in the session JSON.
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    expected_sec = _start_seconds(proc_start)
    if expected_sec is None:
        return True
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        actual_sec = _start_seconds(result.stdout)
        if actual_sec is None:
            return True
        return actual_sec == expected_sec
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True


def get_session_files() -> list[dict]:
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            sessions.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return sessions


def _resolve_name(ai_title: str, json_name: str) -> str:
    return ai_title or json_name or ""


def _normalize_ts(ts: float | int | None) -> float:
    if not ts:
        return 0.0
    return ts / 1000 if ts > 1e12 else float(ts)


def build_sessions_json() -> str:
    session_files = get_session_files()
    db_stats = get_jsonl_stats()

    sessions = []
    seen_sids = set()

    for sf in session_files:
        sid = sf.get("sessionId", "")
        seen_sids.add(sid)
        pid = sf.get("pid", 0)
        db = db_stats.get(sid, {})

        started_at_s = _normalize_ts(sf.get("startedAt", 0))
        last_activity = _normalize_ts(db.get("last_message_at")) or started_at_s
        alive = is_pid_alive(pid, sf.get("procStart", ""))

        sessions.append({
            "sessionId": sid,
            "pid": pid,
            "name": _resolve_name(db.get("ai_title", ""), sf.get("name") or ""),
            "cwd": sf.get("cwd", "") or db.get("cwd", ""),
            "startedAt": started_at_s,
            "lastActivity": last_activity,
            "alive": alive,
            "kind": sf.get("kind", ""),
            "entrypoint": sf.get("entrypoint", ""),
            "totalMessages": db.get("total_messages", 0),
            "userMessages": db.get("user_messages", 0),
            "assistantMessages": db.get("assistant_messages", 0),
            "totalCost": db.get("total_cost", 0),
            "totalDurationMs": db.get("total_duration_ms", 0),
            "model": db.get("model", ""),
        })

    for sid, db in db_stats.items():
        if sid in seen_sids:
            continue
        sessions.append({
            "sessionId": sid,
            "pid": None,
            "name": _resolve_name(db.get("ai_title", ""), ""),
            "cwd": db.get("cwd", ""),
            "startedAt": _normalize_ts(db.get("first_message_at")),
            "lastActivity": _normalize_ts(db.get("last_message_at")),
            "alive": False,
            "kind": "interactive",
            "entrypoint": "",
            "totalMessages": db.get("total_messages", 0),
            "userMessages": db.get("user_messages", 0),
            "assistantMessages": db.get("assistant_messages", 0),
            "totalCost": db.get("total_cost", 0),
            "totalDurationMs": db.get("total_duration_ms", 0),
            "model": db.get("model", ""),
        })

    sessions.sort(key=lambda s: s["lastActivity"] or 0, reverse=True)
    return json.dumps({"sessions": sessions, "timestamp": time.time()})


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == "/api/sessions":
                data = build_sessions_json()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data.encode())
            elif self.path == "/" or self.path == "/index.html":
                html_path = Path(__file__).parent / "index.html"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"Dashboard running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
