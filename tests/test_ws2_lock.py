from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from adgenie.config import Settings
from adgenie.db import Base
from adgenie.models import (
    ActionStatus,
    ActionType,
    Campaign,
    EntityLevel,
    EntityStatus,
    Offer,
    OptimizationAction,
    Platform,
)
from adgenie.money import usd_to_micros
from adgenie.core.orchestrator import Orchestrator


def test_stale_session_cannot_rewrite_a_newer_budget(tmp_path):
    settings = Settings(
        database_url="sqlite://",
        dry_run=False,
        postback_secret="t",
        secret_key="t",
        global_daily_budget_cap_usd=500.0,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'lock.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as first, Session(engine) as second:
        offer = Offer(name="o", destination_url="https://offer.test")
        first.add(offer)
        first.flush()
        campaign = Campaign(
            offer_id=offer.id, platform=Platform.META, name="c",
            status=EntityStatus.ACTIVE, daily_budget_micros=usd_to_micros(100),
        )
        first.add(campaign)
        first.commit()

        second.get(Campaign, campaign.id).daily_budget_micros = usd_to_micros(400)
        second.commit()

        assert campaign.daily_budget_micros == usd_to_micros(100)

        action = OptimizationAction(
            level=EntityLevel.CAMPAIGN, entity_id=campaign.id,
            action=ActionType.INCREASE_BUDGET,
            payload={"from_micros": usd_to_micros(100), "to_micros": usd_to_micros(120)},
        )
        first.add(action)
        runner = Orchestrator(
            first, settings=settings, platform_clients={Platform.META: Mock(dry_run=False)}
        )
        result = runner.apply_action(action)

        assert result is False
        assert "stale" in (action.error or "").lower()
        second.expire_all()
        assert second.get(Campaign, campaign.id).daily_budget_micros == usd_to_micros(400)
        assert action.status is ActionStatus.FAILED
    engine.dispose()


def test_activation_refreshes_untouched_campaign_commitments(tmp_path, settings):
    engine = create_engine(f"sqlite:///{tmp_path / 'activation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as first, Session(engine) as second:
        offer = Offer(name="o", destination_url="https://offer.test")
        first.add(offer)
        first.flush()
        active = Campaign(offer_id=offer.id, platform=Platform.META, name="active",
                          status=EntityStatus.ACTIVE, daily_budget_micros=usd_to_micros(100))
        paused = Campaign(offer_id=offer.id, platform=Platform.META, name="paused",
                          status=EntityStatus.PAUSED, daily_budget_micros=usd_to_micros(200), external_id="paused")
        first.add_all([active, paused])
        first.commit()
        second.get(Campaign, active.id).daily_budget_micros = usd_to_micros(400)
        second.commit()
        assert active.daily_budget_micros == usd_to_micros(100)
        client = Mock(dry_run=False)
        action = OptimizationAction(level=EntityLevel.CAMPAIGN, entity_id=paused.id, action=ActionType.RESUME)
        first.add(action)
        runner = Orchestrator(first, settings=settings, platform_clients={Platform.META: client})
        assert not runner.apply_action(action)
        assert "cap" in action.error
        assert paused.status is EntityStatus.PAUSED
        assert active.daily_budget_micros == usd_to_micros(400)
        assert client.mock_calls == []
    engine.dispose()


def test_approval_refreshes_a_proposal_applied_by_another_session(tmp_path, settings, monkeypatch):
    from fastapi import HTTPException
    from adgenie.api import routes_optimizer

    engine = create_engine(f"sqlite:///{tmp_path / 'approval.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as first, Session(engine) as second:
        action = OptimizationAction(level=EntityLevel.CAMPAIGN, entity_id=1, action=ActionType.PAUSE)
        first.add(action)
        first.commit()
        second.get(OptimizationAction, action.id).status = ActionStatus.APPLIED
        second.commit()
        assert action.status is ActionStatus.PROPOSED
        client = Mock(dry_run=False)
        monkeypatch.setattr(routes_optimizer, "get_settings", lambda: settings)
        monkeypatch.setattr(Orchestrator, "client", lambda *args: client)
        with pytest.raises(HTTPException) as exc:
            routes_optimizer.approve_action(action.id, first)
        assert exc.value.status_code == 409
        assert action.status is ActionStatus.APPLIED
        assert client.mock_calls == []
    engine.dispose()


def test_the_budget_lock_works_on_a_database_with_no_offers(tmp_path):
    """An empty database must serialise budget writers like a full one.

    The lock used to write to the offers table, which does nothing at all
    when no offer exists yet. A fresh deployment would then have had no
    serialisation on its very first budget writes.
    """
    from sqlalchemy import select as sa_select

    from adgenie.db import BUDGET_LOCK_ID, BudgetLock, lock_budget_mutations

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert session.scalars(sa_select(Offer)).all() == []

        lock_budget_mutations(session)

        assert session.scalars(sa_select(BudgetLock.id)).all() == [BUDGET_LOCK_ID]
        # The same transaction may take it again without a second write.
        lock_budget_mutations(session)
        assert session.scalars(sa_select(BudgetLock.id)).all() == [BUDGET_LOCK_ID]
        session.commit()

    # A second transaction reuses the row rather than failing on the key.
    with Session(engine) as session:
        lock_budget_mutations(session)
        assert session.scalars(sa_select(BudgetLock.id)).all() == [BUDGET_LOCK_ID]
        session.commit()
    engine.dispose()


def test_a_second_writer_cannot_read_a_budget_between_lock_and_commit(tmp_path):
    """The lock must block the other writer, not just order two reads."""
    from sqlalchemy import select as sa_select
    from sqlalchemy.exc import OperationalError

    from adgenie.db import lock_budget_mutations

    engine = create_engine(
        f"sqlite:///{tmp_path / 'contended.db'}",
        connect_args={"timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as first, Session(engine) as second:
        offer = Offer(name="o", destination_url="https://offer.test")
        first.add(offer)
        first.commit()

        lock_budget_mutations(first)
        first.add(Campaign(
            offer_id=offer.id, platform=Platform.META, name="c",
            status=EntityStatus.ACTIVE, daily_budget_micros=usd_to_micros(100),
        ))
        first.flush()

        with pytest.raises(OperationalError):
            lock_budget_mutations(second)
        second.rollback()

        first.commit()
        assert second.scalars(sa_select(Campaign)).all() != []
    engine.dispose()
