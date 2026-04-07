#!/usr/bin/env python3
"""Claude Code session dashboard server."""

import json
import os
import signal
import sqlite3
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
DB_PATH = CLAUDE_DIR / "__store.db"
PORT = 7227


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


def get_db_stats() -> dict:
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    stats = {}

    # Message counts, timestamps, and cwd per session
    cur.execute("""
        SELECT
            session_id,
            COUNT(*) as total_messages,
            SUM(CASE WHEN message_type = 'user' THEN 1 ELSE 0 END) as user_messages,
            SUM(CASE WHEN message_type = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
            MIN(timestamp) as first_message_at,
            MAX(timestamp) as last_message_at,
            original_cwd
        FROM base_messages
        GROUP BY session_id
    """)
    for row in cur.fetchall():
        sid = row["session_id"]
        stats[sid] = {
            "total_messages": row["total_messages"],
            "user_messages": row["user_messages"],
            "assistant_messages": row["assistant_messages"],
            "first_message_at": row["first_message_at"],
            "last_message_at": row["last_message_at"],
            "cwd": row["original_cwd"] or "",
        }

    # Cost and duration per session
    cur.execute("""
        SELECT
            bm.session_id,
            SUM(am.cost_usd) as total_cost,
            SUM(am.duration_ms) as total_duration_ms,
            am.model
        FROM assistant_messages am
        JOIN base_messages bm ON am.uuid = bm.uuid
        GROUP BY bm.session_id
    """)
    for row in cur.fetchall():
        sid = row["session_id"]
        if sid in stats:
            stats[sid]["total_cost"] = row["total_cost"]
            stats[sid]["total_duration_ms"] = row["total_duration_ms"]
            stats[sid]["model"] = row["model"]

    conn.close()
    return stats


def build_sessions_json() -> str:
    session_files = get_session_files()
    db_stats = get_db_stats()

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
