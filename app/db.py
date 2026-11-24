"""SQLite storage: conversations, message history and leads.

Plain sqlite3 + SQL on purpose: no ORM magic, easy to read and to query by hand
(`sqlite3 data/bot.db "select * from leads"`).
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    phone          TEXT PRIMARY KEY,          -- customer's WhatsApp number (wa_id)
    profile_name   TEXT,                      -- WhatsApp profile name, if sent
    needs_human    INTEGER NOT NULL DEFAULT 0,
    handoff_reason TEXT,
    handoff_at     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL REFERENCES conversations(phone),
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content       TEXT NOT NULL,
    wa_message_id TEXT UNIQUE,                -- WhatsApp id; used to ignore retries
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id);

CREATE TABLE IF NOT EXISTS leads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    phone            TEXT NOT NULL UNIQUE,    -- one lead per customer, enriched over time
    name             TEXT,
    product_interest TEXT,
    last_message     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- conversations -------------------------------------------------
    def ensure_conversation(self, phone: str, profile_name: str | None = None) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO conversations (phone, profile_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       profile_name = COALESCE(excluded.profile_name, profile_name),
                       updated_at = excluded.updated_at""",
                (phone, profile_name, now, now),
            )

    def get_conversation(self, phone: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE phone = ?", (phone,)).fetchone()
        return dict(row) if row else None

    def flag_needs_human(self, phone: str, reason: str) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """UPDATE conversations
                   SET needs_human = 1, handoff_reason = ?, handoff_at = ?, updated_at = ?
                   WHERE phone = ?""",
                (reason, now, now, phone),
            )

    def resolve_handoff(self, phone: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE conversations
                   SET needs_human = 0, handoff_reason = NULL, handoff_at = NULL, updated_at = ?
                   WHERE phone = ?""",
                (utcnow(), phone),
            )
        return cur.rowcount > 0

    def list_handoffs(self) -> list[dict]:
        """Conversations waiting for a person, with the customer's last message."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT c.phone, c.profile_name, c.handoff_reason, c.handoff_at,
                          (SELECT content FROM messages m
                           WHERE m.phone = c.phone AND m.role = 'user'
                           ORDER BY m.id DESC LIMIT 1) AS last_customer_message
                   FROM conversations c
                   WHERE c.needs_human = 1
                   ORDER BY c.handoff_at ASC"""
            ).fetchall()
        return [dict(r) for r in rows]

    # --- messages ------------------------------------------------------
    def message_exists(self, wa_message_id: str | None) -> bool:
        if not wa_message_id:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM messages WHERE wa_message_id = ?", (wa_message_id,)
            ).fetchone()
        return row is not None

    def add_message(self, phone: str, role: str, content: str, wa_message_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO messages (phone, role, content, wa_message_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (phone, role, content, wa_message_id, utcnow()),
            )

    def recent_messages(self, phone: str, limit: int) -> list[dict]:
        """Last `limit` messages for a phone, oldest first (the order an LLM expects)."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT role, content, created_at FROM messages
                   WHERE phone = ? ORDER BY id DESC LIMIT ?""",
                (phone, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # --- leads ---------------------------------------------------------
    def upsert_lead(self, phone: str, name: str | None, product: str | None, message: str) -> dict:
        """Create the customer's lead, or fill in details we learned since.

        New non-empty values win; we never overwrite a known name/product with NULL.
        """
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO leads (phone, name, product_interest, last_message, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       name = COALESCE(excluded.name, name),
                       product_interest = COALESCE(excluded.product_interest, product_interest),
                       last_message = excluded.last_message,
                       updated_at = excluded.updated_at""",
                (phone, name, product, message, now, now),
            )
            row = conn.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()
        return dict(row)

    def list_leads(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM leads ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]
