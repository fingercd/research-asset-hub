from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from packages.domain.models import (
    Asset,
    AuditEvent,
    Base,
    OutboxEvent,
    Representation,
)
from packages.integrations.html_policy import (
    HtmlPolicyInference,
    apply_html_policy_inferences,
)


def test_html_policy_application_is_audited_and_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        asset = Asset(asset_type="paper", title="HTML policy")
        session.add(asset)
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            representation_type="html",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="html",
            path_ref="paper/index.html",
            attributes={"existing": True},
        )
        session.add(representation)
        session.commit()
        representation_id = representation.id

    inference = HtmlPolicyInference(
        representation_id=representation_id,
        policy="trusted_local",
        content_sha256="a" * 64,
    )
    with Session(engine) as session:
        first = apply_html_policy_inferences(session, [inference])
        session.commit()
    assert first["updated"] == 1
    assert first["retired"] == 0

    with Session(engine) as session:
        stored = session.get(Representation, representation_id)
        assert stored is not None
        assert stored.attributes["existing"] is True
        assert stored.attributes["sandbox_policy"] == "trusted_local"
        assert stored.network_required is False
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1

        second = apply_html_policy_inferences(session, [inference])
        session.commit()
    assert second["updated"] == 0
    assert second["unchanged"] == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    engine.dispose()
