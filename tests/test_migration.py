"""A database created by the previous release must come up under this one.

Runs in a subprocess: the engine is bound at import, and the point is to watch
a cold start against a pre-existing file.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# The schema as deployed before accounts and tiers existed.
OLD_SCHEMA = """
CREATE TABLE accounts (
    id VARCHAR(64) PRIMARY KEY, device_token VARCHAR(64) NOT NULL UNIQUE,
    email VARCHAR(320), stripe_customer_id VARCHAR(64),
    purchased_credits INTEGER NOT NULL, created_at DATETIME NOT NULL
);
CREATE TABLE jobs (
    id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) NOT NULL,
    status VARCHAR(9) NOT NULL, language VARCHAR(16) NOT NULL,
    splitter VARCHAR(16) NOT NULL, model VARCHAR(32) NOT NULL,
    audio_filename VARCHAR(512) NOT NULL, text_filename VARCHAR(512) NOT NULL,
    audio_parts INTEGER NOT NULL, cover_filename VARCHAR(512),
    audio_bytes BIGINT NOT NULL, audio_duration_seconds FLOAT,
    progress FLOAT NOT NULL, stage VARCHAR(128) NOT NULL, error TEXT,
    billed INTEGER NOT NULL, created_at DATETIME NOT NULL,
    started_at DATETIME, finished_at DATETIME
);
CREATE TABLE artifacts (
    id VARCHAR(64) PRIMARY KEY, job_id VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL, filename VARCHAR(512) NOT NULL,
    storage_key VARCHAR(1024) NOT NULL, size_bytes BIGINT NOT NULL,
    created_at DATETIME NOT NULL
);
INSERT INTO accounts VALUES
    ('acct_old', 'tok_old', NULL, NULL, 0, '2026-09-01 00:00:00');
INSERT INTO jobs VALUES
    ('job_old', 'acct_old', 'succeeded', 'ru', 'pysbd', 'tiny', 'a.m4b', 'a.epub',
     1, NULL, 10, 1.0, 1.0, 'Done', NULL, 1, '2026-09-01 00:00:00', NULL, NULL);
"""

PROBE = """
from backend.db import init_db, SessionLocal, Account, Job
init_db(); init_db()  # twice: a restart must be a no-op
with SessionLocal() as s:
    job = s.get(Job, "job_old")
    acct = s.get(Account, "acct_old")
    print(job.tier, job.credit_spent, job.status.value, acct.merged_into, acct.signed_in,
          job.free_credit_spent, acct.free_credits_used, acct.email_verified,
          acct.daily_credit_on, job.daily_credit_on)
"""


def test_old_database_is_upgraded_in_place(tmp_path):
    db = tmp_path / "subplz.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_SCHEMA)
    con.commit()
    con.close()

    env = {**os.environ, "SUBPLZ_WEB_DATA_DIR": str(tmp_path)}
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=root, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    # Old rows read back with the new columns defaulted, nothing lost.
    assert out.stdout.split() == [
        "free", "0", "succeeded", "None", "False", "0", "0", "0", "None", "None",
    ]

    con = sqlite3.connect(db)
    cols = lambda t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}  # noqa: E731
    assert {"tier", "credit_spent", "free_credit_spent", "daily_credit_on",
            "plan_credit_spent"} <= cols("jobs")
    assert {"merged_into", "subscription_id", "subscription_status",
            "subscription_period_end", "free_credits_used", "email_verified",
            "daily_credit_on", "subscription_plan_id",
            "subscription_credits_used"} <= cols("accounts")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"login_tokens", "purchases"} <= tables
    # The unique email index has to exist, or two accounts could share one.
    indexes = {r[1]: r[2] for r in con.execute("PRAGMA index_list(accounts)")}
    assert indexes.get("ix_accounts_email") == 1
    con.close()
