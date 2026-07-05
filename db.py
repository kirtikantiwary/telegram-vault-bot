"""
db.py
Handles all database operations: users, saved messages, and traffic logs.
Uses SQLite for simplicity (swap to Postgres later by changing connection logic).
"""

import sqlite3
from datetime import datetime
from contextlib import contextmanager

DB_PATH = "vault.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        c = conn.cursor()

        # Users table
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT,
                is_banned INTEGER DEFAULT 0
            )
        """)

        # Saved messages (the "vault")
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                content_type TEXT,        -- text, photo, video, document, voice, link
                content_text TEXT,        -- caption or text body
                file_id TEXT,             -- telegram file_id for media
                source_chat TEXT,         -- where it was forwarded from, if known
                tag TEXT,                 -- optional user-defined tag/category
                created_at TEXT,
                FOREIGN KEY (owner_id) REFERENCES users(user_id)
            )
        """)

        # Sharing table (explicit permission-based sharing between users)
        c.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                share_id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER,
                shared_by INTEGER,
                shared_with INTEGER,
                shared_at TEXT,
                FOREIGN KEY (msg_id) REFERENCES messages(msg_id)
            )
        """)

        # Traffic logs (every incoming/outgoing action, separate from content)
        c.execute("""
            CREATE TABLE IF NOT EXISTS traffic_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                direction TEXT,      -- incoming / outgoing
                action TEXT,         -- save, list, retrieve, share, admin_view, etc.
                detail TEXT,
                timestamp TEXT
            )
        """)

        # Admins table
        c.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)


# ---------- USER OPERATIONS ----------

def upsert_user(user_id, username, first_name):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, datetime.utcnow().isoformat())
            )


def is_banned(user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        return bool(row and row["is_banned"])


# ---------- MESSAGE OPERATIONS ----------

def save_message(owner_id, content_type, content_text, file_id, source_chat, tag=None):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO messages (owner_id, content_type, content_text, file_id, source_chat, tag, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (owner_id, content_type, content_text, file_id, source_chat, tag, datetime.utcnow().isoformat()))
        return c.lastrowid


def list_messages(owner_id, limit=20):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM messages WHERE owner_id=? ORDER BY created_at DESC LIMIT ?
        """, (owner_id, limit))
        return c.fetchall()


def search_messages(owner_id, keyword):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM messages WHERE owner_id=? AND content_text LIKE ?
            ORDER BY created_at DESC
        """, (owner_id, f"%{keyword}%"))
        return c.fetchall()


def get_message(msg_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,))
        return c.fetchone()


def delete_message(msg_id, owner_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM messages WHERE msg_id=? AND owner_id=?", (msg_id, owner_id))
        return c.rowcount > 0


# ---------- SHARING (explicit permission-based) ----------

def share_message(msg_id, shared_by, shared_with):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO shares (msg_id, shared_by, shared_with, shared_at)
            VALUES (?, ?, ?, ?)
        """, (msg_id, shared_by, shared_with, datetime.utcnow().isoformat()))


def get_shared_with_me(user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT m.*, s.shared_by, s.shared_at FROM shares s
            JOIN messages m ON m.msg_id = s.msg_id
            WHERE s.shared_with = ?
            ORDER BY s.shared_at DESC
        """, (user_id,))
        return c.fetchall()


# ---------- TRAFFIC LOGGING ----------

def log_traffic(user_id, direction, action, detail=""):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO traffic_logs (user_id, direction, action, detail, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, direction, action, detail, datetime.utcnow().isoformat()))


# ---------- ADMIN OPERATIONS ----------

def is_admin(user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return c.fetchone() is not None


def add_admin(user_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))


def get_stats():
    with get_conn() as conn:
        c = conn.cursor()
        stats = {}
        c.execute("SELECT COUNT(*) as n FROM users")
        stats["total_users"] = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM messages")
        stats["total_messages"] = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM traffic_logs WHERE direction='incoming'")
        stats["incoming_events"] = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM traffic_logs WHERE direction='outgoing'")
        stats["outgoing_events"] = c.fetchone()["n"]

        c.execute("""
            SELECT content_type, COUNT(*) as n FROM messages
            GROUP BY content_type ORDER BY n DESC
        """)
        stats["by_type"] = {row["content_type"]: row["n"] for row in c.fetchall()}

        c.execute("""
            SELECT user_id, COUNT(*) as n FROM messages
            GROUP BY user_id ORDER BY n DESC LIMIT 5
        """)
        stats["top_users"] = [(row["user_id"], row["n"]) for row in c.fetchall()]

        return stats


def get_recent_traffic(limit=30):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM traffic_logs ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        return c.fetchall()


def get_all_messages(limit=50, offset=0):
    """Admin: raw view into ALL stored messages, across all users."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT m.*, u.username FROM messages m
            LEFT JOIN users u ON u.user_id = m.owner_id
            ORDER BY m.created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset))
        return c.fetchall()


def get_user_messages_admin(target_user_id):
    """Admin: view a specific user's full vault."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM messages WHERE owner_id=? ORDER BY created_at DESC
        """, (target_user_id,))
        return c.fetchall()


def ban_user(user_id, banned=True):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
