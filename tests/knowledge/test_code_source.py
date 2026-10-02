from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api import routes_v1
from apps.api.problem import ApiProblem
from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Asset, Base, Representation, Source


@pytest.mark.parametrize("unavailable", ["disabled_source", "hidden_owner", "deleted_owner"])
def test_code_context_denies_unavailable_source_or_owner(tmp_path, monkeypatch, unavailable):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('ok')", encoding="utf-8")
    with Session(engine) as session:
        source = Source(
            alias="code-fixture",
            label="code",
            adapter="generic_manifest",
            root_path=str(tmp_path),
            config={},
            enabled=True,
        )
        asset = Asset(asset_type="repository", title="demo")
        session.add_all([source, asset])
        session.flush()
        rep = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="repository",
            role="original",
            materialization_kind="directory",
            availability="available",
            root_key="repository",
            path_ref="repo",
        )
        session.add(rep)
        session.commit()

        @contextmanager
        def database_session():
            yield session

        class Database:
            session = staticmethod(database_session)

        monkeypatch.setattr(routes_v1, "_database", lambda _: Database())
        monkeypatch.setattr(
            routes_v1,
            "_source_registry",
            lambda _: SourceRegistry({("code-fixture", "repository"): tmp_path}),
        )
        assert routes_v1._local_code_context(None, rep.id)[1] == root
        if unavailable == "disabled_source":
            source.enabled = False
        elif unavailable == "hidden_owner":
            asset.attributes = {"system_hidden": True}
        else:
            from datetime import UTC, datetime

            asset.deleted_at = datetime.now(UTC)
        session.flush()
        with pytest.raises(ApiProblem):
            routes_v1._local_code_context(None, rep.id)
