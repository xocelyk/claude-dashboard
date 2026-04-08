#!/usr/bin/env python3
"""Claude Code session dashboard server."""

import json
import os
import time
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PORT = 7227

# Approximate cost per token by model (USD) — input/output
# Uses cache-read pricing where applicable
COST_PER_TOKEN = {
    "input": 15.0 / 1_000_000,   # $15 per 1M input tokens (Opus default)
    "output": 75.0 / 1_000_000,  # $75 per 1M output tokens (Opus default)
    "cache_read": 1.5 / 1_000_000,  # $1.50 per 1M cached input tokens
    "cache_write": 18.75 / 1_000_000,  # $18.75 per 1M cache creation tokens
}


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_session_files() -> list[dict]:
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            data["_file"] = f.name
            sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return sessions


def _iso_to_epoch(ts_str: str) -> float:
    """Convert ISO 8601 timestamp string to epoch seconds."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _estimate_cost(usage: dict) -> float:
    """Estimate cost in USD from a usage dict."""
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    # Non-cached input tokens = total input minus cached
    plain_input = max(0, input_tokens - cache_read)
    return (
        plain_input * COST_PER_TOKEN["input"]
        + output_tokens * COST_PER_TOKEN["output"]
        + cache_read * COST_PER_TOKEN["cache_read"]
        + cache_write * COST_PER_TOKEN["cache_write"]
    )


def _find_jsonl(session_id: str) -> Path | None:
    """Find the JSONL file for a given session ID."""
    if not PROJECTS_DIR.exists():
        return None
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        # Skip subagent files
        if "subagents" not in str(jsonl):
            return jsonl
    return None


def get_jsonl_stats() -> dict:
    """Parse JSONL files to get message counts, cost, duration, timestamps per session."""
    stats = {}
    if not PROJECTS_DIR.exists():
        return stats

    # Find all non-subagent JSONL files
    for jsonl_path in PROJECTS_DIR.rglob("*.jsonl"):
        if "subagents" in str(jsonl_path):
            continue

        session_id = jsonl_path.stem
        total = 0
        user_msgs = 0
        assistant_msgs = 0
        first_ts = None
        last_ts = None
        total_cost = 0.0
        model = ""
        cwd = ""
        first_assistant_ts = None
        last_assistant_ts = None

        try:
            with open(jsonl_path) as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type", "")
                    ts_str = msg.get("timestamp", "")
                    ts = _iso_to_epoch(ts_str) if ts_str else 0

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
                            usage = inner.get("usage", {})
                            if usage:
                                total_cost += _estimate_cost(usage)
                            if inner.get("model"):
                                model = inner["model"]
                            if ts:
                                if first_assistant_ts is None or ts < first_assistant_ts:
                                    first_assistant_ts = ts
                                if last_assistant_ts is None or ts > last_assistant_ts:
                                    last_assistant_ts = ts
        except OSError:
            continue

        if total > 0:
            # Duration: time between first and last assistant message
            duration_ms = 0
            if first_assistant_ts and last_assistant_ts:
                duration_ms = int((last_assistant_ts - first_assistant_ts) * 1000)

            stats[session_id] = {
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

    return stats


def build_sessions_json() -> str:
    session_files = get_session_files()
    db_stats = get_jsonl_stats()

    sessions = []
    seen_sids = set()

    # Sessions with JSON files (have full metadata)
    for sf in session_files:
        sid = sf.get("sessionId", "")
        seen_sids.add(sid)
        pid = sf.get("pid", 0)
        db = db_stats.get(sid, {})

        started_at = sf.get("startedAt", 0)
        # startedAt is in milliseconds, convert to seconds
        started_at_s = started_at / 1000 if started_at > 1e12 else started_at

        last_activity = db.get("last_message_at", started_at_s)
        # DB timestamps might be in seconds already
        if last_activity and last_activity > 1e12:
            last_activity = last_activity / 1000

        alive = is_pid_alive(pid)

        sessions.append({
            "sessionId": sid,
            "pid": pid,
            "name": sf.get("name", ""),
            "cwd": sf.get("cwd", ""),
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

    # Historical sessions from DB that no longer have JSON files
    for sid, db in db_stats.items():
        if sid in seen_sids:
            continue

        first_ts = db.get("first_message_at", 0)
        last_ts = db.get("last_message_at", 0)
        # Normalize timestamps
        if first_ts and first_ts > 1e12:
            first_ts = first_ts / 1000
        if last_ts and last_ts > 1e12:
            last_ts = last_ts / 1000

        sessions.append({
            "sessionId": sid,
            "pid": None,
            "name": "",
            "cwd": db.get("cwd", ""),
            "startedAt": first_ts,
            "lastActivity": last_ts,
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

    # Sort by last activity descending
    sessions.sort(key=lambda s: s["lastActivity"] or 0, reverse=True)

    return json.dumps({"sessions": sessions, "timestamp": time.time()})


class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
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

    def log_message(self, format, *args):
        pass  # Suppress request logging


def main():
    server = HTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"Dashboard running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
