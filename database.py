import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "referrals.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id          INTEGER PRIMARY KEY,
                username          TEXT,
                full_name        TEXT,
                invited_by       INTEGER,
                channel_verified INTEGER NOT NULL DEFAULT 0,
                joined_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notified_milestones (
                user_id   INTEGER NOT NULL,
                milestone INTEGER NOT NULL,
                PRIMARY KEY (user_id, milestone)
            )
        """)
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()


async def register_user(user_id: int, username: str, full_name: str, invited_by: int = None):
    """Insert new user or update name/username. invited_by is only set on first insert."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name, invited_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name
        """, (user_id, username, full_name, invited_by))
        await db.commit()


async def verify_channel_member(user_id: int):
    """Mark user as channel-verified. Returns True if this is a new verification."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_verified FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        was_verified = bool(row["channel_verified"])
        if not was_verified:
            await db.execute(
                "UPDATE users SET channel_verified = 1 WHERE user_id = ?", (user_id,)
            )
            await db.commit()
        return not was_verified  # True = newly verified now


async def get_invite_count(user_id: int) -> int:
    """Count verified referrals — only users who are in the channel count."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE invited_by = ? AND channel_verified = 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def milestone_already_notified(user_id: int, milestone: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM notified_milestones WHERE user_id = ? AND milestone = ?",
            (user_id, milestone)
        ) as cur:
            return await cur.fetchone() is not None


async def mark_milestone_notified(user_id: int, milestone: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO notified_milestones (user_id, milestone) VALUES (?, ?)",
            (user_id, milestone)
        )
        await db.commit()


async def get_unverified_users() -> list[dict]:
    """Return all users who have not yet been channel-verified."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, invited_by FROM users WHERE channel_verified = 0"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        # CSAK a valós Telegram ID-kat kérjük le (amik nem NULL-ok és nem 0-nál kisebbek/fantomok)
        async with db.execute("SELECT user_id FROM users WHERE user_id IS NOT NULL AND username != 'manual_bonus'") as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_all_users_with_counts(limit: int = 50):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.channel_verified,
                u.joined_at,
                COUNT(r.user_id) AS invite_count
            FROM users u
            LEFT JOIN users r ON r.invited_by = u.user_id AND r.channel_verified = 1
            GROUP BY u.user_id
            ORDER BY invite_count DESC
            LIMIT ?
        """, (limit,)) as cur:
            return await cur.fetchall()


async def add_manual_points(user_id: int, amount: int):
    """Manuálisan hozzáad pontot (ál-felhasználók generálásával a háttérben)."""
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(amount):
            # Létrehozunk láthatatlan "fantom" felhasználókat, akik az illetőre mutatnak és igazoltak
            await db.execute("""
                INSERT INTO users (user_id, username, full_name, invited_by, channel_verified)
                VALUES (NULL, 'manual_bonus', 'Manuális Pont', ?, 1)
            """, (user_id,))
        await db.commit()


async def remove_manual_points(user_id: int, amount: int):
    """Manuálisan levon pontot a felhasználótól (fantomok vagy igazolások törlésével)."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Kiválasztunk 'amount' számú olyan felhasználót, akit ő hívott meg és aktív, majd lekapcsoljuk őket
        async with db.execute(
            "SELECT user_id FROM users WHERE invited_by = ? AND channel_verified = 1 LIMIT ?",
            (user_id, amount)
        ) as cur:
            rows = await cur.fetchall()
        
        for row in rows:
            await db.execute(
                "UPDATE users SET channel_verified = 0 WHERE user_id = ?", (row[0],)
            )
        await db.commit()


async def unverify_channel_member(user_id: int):
    """Leveszi a felhasználó hitelesítését (így a meghívója pontja automatikusan csökken)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT invited_by, full_name, username, channel_verified FROM users WHERE user_id = ?", 
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            
        if not row:
            return False, None, ""
            
        was_verified = bool(row["channel_verified"])
        if not was_verified:
            return False, None, ""

        referrer_id = row["invited_by"]
        full_name = row["full_name"] or row["username"] or f"User {user_id}"

        # Átállítjuk nem igazoltra
        await db.execute(
            "UPDATE users SET channel_verified = 0 WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        
        return True, referrer_id, full_name
