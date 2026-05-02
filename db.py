from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

_db_url = settings.auth_db_url
_connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def apply_sqlite_migrations() -> None:
    """Add columns missing from older DB files (SQLite only)."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        col_names = {r[1] for r in rows}
        if "is_active" not in col_names:
            try:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")
                )
            except OperationalError as e:
                # Concurrent workers may both attempt ALTER; second sees column already added.
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        if "group_id" not in col_names:
            try:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN group_id INTEGER REFERENCES user_groups(id)")
                )
            except OperationalError as e:
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        if "max_stored_large_rows" not in col_names:
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN max_stored_large_rows INTEGER"))
            except OperationalError as e:
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        g_rows = conn.execute(text("PRAGMA table_info(user_groups)")).fetchall()
        g_col_names = {r[1] for r in g_rows}
        if "max_stored_large_rows" not in g_col_names:
            try:
                conn.execute(
                    text("ALTER TABLE user_groups ADD COLUMN max_stored_large_rows INTEGER")
                )
            except OperationalError as e:
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        if "subscription_cycle_started_at" not in col_names:
            try:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN subscription_cycle_started_at DATETIME")
                )
            except OperationalError as e:
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET subscription_cycle_started_at = datetime('now')
                    WHERE COALESCE(is_admin, 0) = 0
                      AND subscription_cycle_started_at IS NULL
                    """
                )
            )
        rows_u = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        ucols = {r[1] for r in rows_u}
        if "display_name" not in ucols:
            try:
                conn.execute(
                    text(
                        "ALTER TABLE users ADD COLUMN display_name VARCHAR(200) NOT NULL DEFAULT ''"
                    )
                )
            except OperationalError as e:
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        rows_u2 = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        ucols2 = {r[1] for r in rows_u2}
        if "subscription_early_renew_at" not in ucols2:
            try:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN subscription_early_renew_at DATETIME")
                )
            except OperationalError as e:
                if "duplicate column" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        try:
            conn.execute(
                text("ALTER TABLE gemini_usage_events ADD COLUMN billing_cycle_start_at DATETIME")
            )
        except OperationalError as e:
            msg = str(getattr(e, "orig", None) or e).lower()
            if "duplicate column" not in msg and "no such table" not in msg:
                raise
        try:
            conn.execute(
                text(
                    """
                    UPDATE gemini_usage_events
                    SET billing_cycle_start_at = (
                        SELECT u.subscription_cycle_started_at
                        FROM users u
                        WHERE u.id = gemini_usage_events.user_id
                    )
                    WHERE user_id IS NOT NULL AND billing_cycle_start_at IS NULL
                    """
                )
            )
        except OperationalError:
            pass
        # Provider keys moved to Redis pools — drop legacy table if present.
        try:
            conn.execute(text("DROP TABLE IF EXISTS provider_api_key_settings"))
        except OperationalError:
            pass


def apply_postgres_auth_migrations() -> None:
    """user_groups + users.group_id when auth DB is PostgreSQL."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS user_groups (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(200) NOT NULL UNIQUE
                )
                """
            )
        )
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users'
                  AND column_name = 'group_id'
                """
            )
        ).fetchone()
        if not row:
            try:
                conn.execute(
                    text(
                        """
                        ALTER TABLE users ADD COLUMN group_id INTEGER
                        REFERENCES user_groups(id) ON DELETE SET NULL
                        """
                    )
                )
            except OperationalError as e:
                if "already exists" not in str(getattr(e, "orig", None) or e).lower():
                    raise
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users'
                  AND column_name = 'max_stored_large_rows'
                """
            )
        ).fetchone()
        if not row:
            conn.execute(text("ALTER TABLE users ADD COLUMN max_stored_large_rows INTEGER"))
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'user_groups'
                  AND column_name = 'max_stored_large_rows'
                """
            )
        ).fetchone()
        if not row:
            conn.execute(text("ALTER TABLE user_groups ADD COLUMN max_stored_large_rows INTEGER"))
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users'
                  AND column_name = 'subscription_cycle_started_at'
                """
            )
        ).fetchone()
        if not row:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN subscription_cycle_started_at TIMESTAMPTZ")
            )
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET subscription_cycle_started_at = NOW()
                    WHERE is_admin = false
                      AND subscription_cycle_started_at IS NULL
                    """
                )
            )
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users'
                  AND column_name = 'display_name'
                """
            )
        ).fetchone()
        if not row:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN display_name VARCHAR(200) NOT NULL DEFAULT ''")
            )
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users'
                  AND column_name = 'subscription_early_renew_at'
                """
            )
        ).fetchone()
        if not row:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN subscription_early_renew_at TIMESTAMPTZ")
            )
        row = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'gemini_usage_events'
                """
            )
        ).fetchone()
        if row:
            row = conn.execute(
                text(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'gemini_usage_events'
                      AND column_name = 'billing_cycle_start_at'
                    """
                )
            ).fetchone()
            if not row:
                conn.execute(
                    text(
                        "ALTER TABLE gemini_usage_events ADD COLUMN billing_cycle_start_at TIMESTAMPTZ"
                    )
                )
            conn.execute(
                text(
                    """
                    UPDATE gemini_usage_events AS g
                    SET billing_cycle_start_at = u.subscription_cycle_started_at
                    FROM users u
                    WHERE g.user_id = u.id
                      AND g.user_id IS NOT NULL
                      AND g.billing_cycle_start_at IS NULL
                    """
                )
            )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
