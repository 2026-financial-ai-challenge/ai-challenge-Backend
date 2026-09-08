import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

# Where _configure_test_database() decided the suite is allowed to write.
# _engine_is_bound_to_the_test_database() below holds the run to it.
_TEST_URL = None


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

    global _TEST_URL
    _TEST_URL = test_url

    resolved_test_url = test_url.render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = resolved_test_url
    os.environ.pop("DATABASE_PUBLIC_URL", None)

    backend_dir = Path(__file__).resolve().parents[1]
    alembic_config = Config(str(backend_dir / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_dir / "alembic"))
    command.upgrade(alembic_config, "head")


_configure_test_database()


def _target(url) -> tuple:
    """The part of a URL that decides which database gets written to."""
    return (url.host, url.port, url.database)


@pytest.fixture(scope="session", autouse=True)
def _engine_is_bound_to_the_test_database() -> None:
    """Stop the run if anything re-pointed the suite at a real database.

    _configure_test_database() runs at import time, but the app is imported
    later -- during collection -- and it loads backend/.env, which carries the
    deployed DATABASE_URL. Any future path that puts that value back (an
    override=True, a stray load_dotenv, a fixture) would have the suite
    creating and dropping rows in the deployed database with nothing to say
    so. The engine is what queries actually go through, so check that; the
    environment matters too, for anything that builds an engine of its own.
    """
    from app.database import engine

    problems = []
    if _target(engine.url) != _target(_TEST_URL):
        problems.append(
            f"engine is bound to {engine.url.host}/{engine.url.database}"
        )
    live = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL") or ""
    if live and _target(make_url(live)) != _target(_TEST_URL):
        env_url = make_url(live)
        problems.append(f"DATABASE_URL now points at {env_url.host}/{env_url.database}")
    if problems:
        pytest.exit(
            "Refusing to run against a database the suite did not prepare: "
            + "; ".join(problems)
            + f". Expected {_TEST_URL.host}/{_TEST_URL.database}.",
            returncode=2,
        )
