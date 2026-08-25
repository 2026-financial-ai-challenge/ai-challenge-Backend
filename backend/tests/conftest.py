import os
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url


def _configure_test_database() -> None:
    source_url = os.getenv("DATABASE_URL")
    if not source_url:
        raise RuntimeError("DATABASE_URL is required to run the test suite")

    parsed = make_url(source_url)
    explicit_test_url = os.getenv("TEST_DATABASE_URL")
    if explicit_test_url:
        test_url = make_url(explicit_test_url)
    else:
        if parsed.host not in {"db", "localhost", "127.0.0.1"}:
            raise RuntimeError(
                "Refusing to derive a test database from a non-local DATABASE_URL. "
                "Set TEST_DATABASE_URL explicitly."
            )
        database = parsed.database or "safety_phishing_call"
        test_url = parsed.set(database=f"{database}_test")

    test_database = test_url.database
    if not test_database:
        raise RuntimeError("TEST_DATABASE_URL must include a database name")
    admin_url = test_url.set(database="postgres")

    with psycopg.connect(admin_url.render_as_string(hide_password=False), autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (test_database,),
        ).fetchone()
        if exists is None:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_database))
            )

    resolved_test_url = test_url.render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = resolved_test_url
    os.environ.pop("DATABASE_PUBLIC_URL", None)

    backend_dir = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(backend_dir / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_dir / "alembic"))
    command.upgrade(alembic_config, "head")


_configure_test_database()
