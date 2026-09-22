"""tools/copy_database.py: a copy has every row of every table, unchanged."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.db import Account, Artifact, Base, Job, JobStatus, Purchase
from tools.copy_database import copy_database

T0 = datetime(2026, 9, 22, tzinfo=timezone.utc)
text = st.text(st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), max_size=20)


@st.composite
def databases(draw):
    accounts = []
    for a in range(draw(st.integers(1, 4))):
        account = Account(
            id=f"acct_{a}", device_token=f"dev_{a}",
            email=draw(st.none() | st.just(f"reader{a}@example.com")),
            purchased_credits=draw(st.integers(0, 500)),
            daily_credit_on=draw(st.none() | st.dates(date(2026, 1, 1), date(2027, 1, 1))),
            created_at=T0 + timedelta(seconds=draw(st.integers(0, 10**6))),
        )
        accounts.append(account)
        for j in range(draw(st.integers(0, 3))):
            job_id = f"job_{a}_{j}"
            accounts.append(Job(
                id=job_id, account_id=account.id, status=draw(st.sampled_from(list(JobStatus))),
                language="ja", splitter="pysbd", model="tiny",
                audio_filename=draw(text), text_filename=draw(text),
                audio_duration_seconds=draw(st.none() | st.floats(0, 1e5)),
                progress=draw(st.floats(0, 1)), error=draw(st.none() | text),
            ))
            accounts.append(Artifact(id=f"art_{a}_{j}", job_id=job_id, kind="srt",
                                     filename=draw(text), storage_key=f"{job_id}/out.srt"))
        if draw(st.booleans()):
            accounts.append(Purchase(id=f"buy_{a}", account_id=account.id, plan_id="pack10",
                                     credits=10, amount_cents=499, stripe_session_id=f"cs_{a}"))
    return accounts


def rows(url: str) -> dict[str, list[dict]]:
    engine = create_engine(url)
    with engine.connect() as c:
        return {t.name: sorted((dict(r._mapping) for r in c.execute(select(t))), key=repr)
                for t in Base.metadata.sorted_tables}


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(objects=databases())
def test_the_copy_has_every_row_unchanged(tmp_path_factory, objects):
    tmp = tmp_path_factory.mktemp("copy")
    source, target = f"sqlite:///{tmp / 'source.db'}", f"sqlite:///{tmp / 'target.db'}"
    engine = create_engine(source)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(objects)
        s.commit()

    counts = copy_database(source, target)

    assert rows(target) == rows(source)
    assert counts["accounts"] == len([o for o in objects if isinstance(o, Account)])


def test_the_copy_refuses_a_target_with_rows(tmp_path):
    source, target = f"sqlite:///{tmp_path / 's.db'}", f"sqlite:///{tmp_path / 't.db'}"
    for url in (source, target):
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            s.add(Account(id="acct_x", device_token="dev_x"))
            s.commit()
    with pytest.raises(RuntimeError, match="accounts"):
        copy_database(source, target)
