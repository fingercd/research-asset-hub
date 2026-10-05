"""SQL catalog selection; full asset objects are loaded only after pagination."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Date,
    Integer,
    String,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    true,
    union,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, aliased, defer

from apps.api.domain_views import annotate_presentation_domains, presentation_domains
from packages.domain.models import (
    Annotation,
    Asset,
    ExternalIdentifierModel,
    ReadingState,
    Relation,
    Representation,
)
from packages.domain.platform_models import SourceRecordAsset
from packages.domain.serving_scope import active_representation_clause


def _postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def json_values(session: Session, column: Any, name: str) -> Any:
    function = func.jsonb_array_elements_text if _postgres(session) else func.json_each
    values = function(column).table_valued("value")
    return values.render_derived(name=name) if _postgres(session) else values.alias(name)


def hidden_clause(session: Session, asset: Any = Asset) -> Any:
    value = asset.attributes["system_hidden"].as_string()
    if _postgres(session):
        # ->> returns "true" for both JSON true and the string "true", but "1" for 1.
        # One extraction also avoids decompressing the large audit document four times.
        return func.lower(value) == "true"
    kind = func.json_type(asset.attributes, "$.system_hidden")
    return or_(
        and_(kind == "string", func.lower(value) == "true"),
        and_(
            kind.in_(["boolean", "true"]), asset.attributes["system_hidden"].as_boolean().is_(True)
        ),
    )


def visible_clause(session: Session, asset: Any = Asset) -> Any:
    return and_(asset.deleted_at.is_(None), ~func.coalesce(hidden_clause(session, asset), False))


def _generation_topic(session: Session, asset: Any) -> Any:
    category = func.coalesce(asset.attributes["category"].as_string(), "")
    title = func.lower(func.coalesce(asset.title, ""))
    tags = json_values(session, asset.tags, "topic_tags")
    video = or_(
        category.startswith("video_", autoescape=True),
        and_(category == "survey", title.contains("video")),
        select(1)
        .select_from(tags)
        .where(func.lower(tags.c.value) == "video-deleted-review")
        .exists(),
        and_(title.contains("video"), category.not_in(["image_core", "image_supplement"])),
    )
    return case((video, literal("video-generation")), else_=literal("image-editing"))


def domain_membership(session: Session) -> Any:
    """One row per public domain and asset, including source-paper inheritance."""
    domains = json_values(session, Asset.domain_slugs, "source_domains")
    base = select(Asset.id.label("asset_id"), domains.c.value.label("slug")).join(domains, true())
    ordinary = base.where(domains.c.value.not_in(["generation", "navigation-compression"]))
    navigation = (
        select(Asset.id.label("asset_id"), literal("embodied-ai-compression").label("slug"))
        .join(domains, true())
        .where(domains.c.value == "navigation-compression")
    )

    repository = aliased(Asset, name="topic_repository")
    paper = aliased(Asset, name="topic_paper")
    repo_link = aliased(SourceRecordAsset, name="topic_repo_link")
    paper_link = aliased(SourceRecordAsset, name="topic_paper_link")
    paper_domains = json_values(session, paper.domain_slugs, "topic_paper_domains")
    repository_domains = json_values(session, repository.domain_slugs, "topic_repository_domains")
    inherited = (
        select(
            repository.id.label("asset_id"),
            case(
                (paper_domains.c.value == "generation", _generation_topic(session, paper)),
                else_=paper_domains.c.value,
            ).label("slug"),
        )
        .select_from(repository)
        .join(repository_domains, true())
        .join(repo_link, repo_link.asset_id == repository.id)
        .join(paper_link, paper_link.source_record_id == repo_link.source_record_id)
        .join(paper, paper.id == paper_link.asset_id)
        .join(paper_domains, true())
        .where(
            repository.asset_type == "repository",
            repository_domains.c.value == "generation",
            repo_link.deleted_at.is_(None),
            paper_link.deleted_at.is_(None),
            paper.asset_type == "paper",
            paper_domains.c.value.in_(["generation", "image-editing", "video-generation"]),
        )
    ).cte("inherited_topics")
    generation = (
        select(Asset.id.label("asset_id"), _generation_topic(session, Asset).label("slug"))
        .join(domains, true())
        .where(
            domains.c.value == "generation",
            ~select(1).select_from(inherited).where(inherited.c.asset_id == Asset.id).exists(),
        )
    )
    inherited_generation = (
        select(Asset.id.label("asset_id"), inherited.c.slug)
        .select_from(Asset)
        .join(domains, true())
        .join(inherited, inherited.c.asset_id == Asset.id)
        .where(domains.c.value == "generation")
    )
    # PostgreSQL may push a topic filter beneath the generation JSON-array gate.
    # That evaluates category/title predicates against every large attributes document.
    # Materialize the small domain projection once before applying consumer filters.
    return (
        union(ordinary, navigation, generation, inherited_generation)
        .cte("presentation_domains")
        .prefix_with("MATERIALIZED", dialect="postgresql")
    )


def confirmed_links(session: Session) -> Any:
    paper = aliased(Asset, name="unit_paper")
    repository = aliased(Asset, name="unit_repository")
    return (
        select(
            Relation.subject_asset_id.label("paper_id"),
            Relation.object_asset_id.label("repository_id"),
        )
        .join(paper, paper.id == Relation.subject_asset_id)
        .join(repository, repository.id == Relation.object_asset_id)
        .where(
            Relation.relation_type == "implemented_by",
            Relation.confirmation_status == "confirmed",
            Relation.deleted_at.is_(None),
            paper.asset_type == "paper",
            repository.asset_type == "repository",
            visible_clause(session, paper),
            visible_clause(session, repository),
        )
        .distinct()
        .cte("confirmed_unit_links")
    )


def reading_status() -> Any:
    return func.coalesce(
        select(ReadingState.status)
        .where(ReadingState.asset_id == Asset.id, ReadingState.user_id == "local")
        .scalar_subquery(),
        "unread",
    )


def action_exists(kind: str, asset_id: Any = Asset.id) -> Any:
    types = {
        "pdf": ["pdf"],
        "html": ["html", "html_document"],
        "code": ["source_code", "repository", "notebook"],
        "guide": ["guide", "analysis", "markdown"],
    }
    predicate: Any = Representation.representation_type.in_(types[kind])
    if kind == "guide":
        predicate = or_(predicate, Representation.role == "guide")
    elif kind == "html":
        predicate = and_(
            predicate,
            or_(
                Representation.role != "guide",
                Representation.attributes["unified_guide"].as_boolean().is_(True),
            ),
        )
    return (
        select(1)
        .select_from(Representation)
        .where(
            Representation.asset_id == asset_id,
            Representation.deleted_at.is_(None),
            Representation.availability == "available",
            active_representation_clause(),
            predicate,
        )
        .exists()
    )


def publication_day(session: Session) -> Any:
    """Validate an ISO calendar prefix before casting external metadata to a date."""
    raw = func.trim(func.coalesce(Asset.attributes["first_public_date"].as_string(), ""))
    day = func.substr(raw, 1, 10)
    if not _postgres(session):
        valid = func.date(day, "+0 days") == day
        return case((valid, day), else_=None)
    # All integer casts are inside a format guard, so malformed metadata stays a fallback.
    year = cast(func.substr(day, 1, 4), Integer)
    month = cast(func.substr(day, 6, 2), Integer)
    number = cast(func.substr(day, 9, 2), Integer)
    leap = or_(year % 400 == 0, and_(year % 4 == 0, year % 100 != 0))
    maximum = case(
        (month == 2, case((leap, 29), else_=28)), (month.in_([4, 6, 9, 11]), 30), else_=31
    )
    calendar_valid = and_(year.between(1, 9999), month.between(1, 12), number.between(1, maximum))
    return case(
        (
            day.op("~")(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
            case((calendar_valid, cast(day, Date)), else_=None),
        ),
        else_=None,
    )


def metadata_match(query: str, tokens: set[str]) -> Any:
    columns = [Asset.title, Asset.title_zh, Asset.abstract]
    terms = [query.casefold(), *sorted(tokens)]
    return or_(
        *(
            func.lower(func.coalesce(column, "")).contains(term, autoescape=True)
            for column in columns
            for term in terms
        ),
        select(1)
        .select_from(ExternalIdentifierModel)
        .where(
            ExternalIdentifierModel.asset_id == Asset.id,
            ExternalIdentifierModel.scheme.in_(["doi", "arxiv", "openreview", "isbn"]),
            or_(
                *(
                    func.lower(ExternalIdentifierModel.canonical).contains(term, autoescape=True)
                    for term in terms
                )
            ),
        )
        .exists(),
    )


def catalog_selection(
    session: Session,
    *,
    memberships: Any,
    links: Any,
    grouped: bool,
    query: str,
    matched_ids: list[UUID] | None,
    tokens: set[str],
    domain: str | None,
    asset_type: str | None,
    status: str | None,
    author: str | None,
    year_from: int | None,
    year_to: int | None,
    date_from: date | None,
    date_to: date | None,
    tags: list[str] | None,
    has_pdf: bool | None,
    has_html: bool | None,
    has_code: bool | None,
    has_guide: bool | None,
    has_annotation: bool | None,
) -> Any:
    statement = select(Asset.id).where(visible_clause(session))
    in_domain = Asset.id.in_(
        select(memberships.c.asset_id)
        .select_from(memberships)
        .where(memberships.c.slug == domain)
        .distinct()
    )
    related_domain = (
        select(1)
        .select_from(links)
        .join(memberships, memberships.c.asset_id == links.c.repository_id)
        .where(links.c.paper_id == Asset.id, memberships.c.slug == domain)
        .exists()
    )
    matched = (
        Asset.id.in_(matched_ids) if matched_ids is not None else metadata_match(query, tokens)
    )
    if grouped:
        related_match = (
            select(1)
            .select_from(links)
            .where(links.c.paper_id == Asset.id, links.c.repository_id.in_(matched_ids or []))
            .exists()
        )
        # Build fallback repository hits separately: the outer asset must remain the unit primary.
        if query.strip() and matched_ids is None:
            fallback_ids = (
                select(Asset.id)
                .where(visible_clause(session), metadata_match(query, tokens))
                .cte("metadata_matches")
            )
            matched = Asset.id.in_(select(fallback_ids.c.id))
            related_match = (
                select(1)
                .select_from(links)
                .where(
                    links.c.paper_id == Asset.id,
                    links.c.repository_id.in_(select(fallback_ids.c.id)),
                )
                .exists()
            )
        paper = Asset.asset_type == "paper"
        if query.strip():
            paper = and_(paper, or_(matched, related_match))
            independent = and_(
                Asset.asset_type == "repository",
                matched,
            )
            statement = statement.where(or_(paper, independent) if asset_type != "paper" else paper)
        else:
            statement = statement.where(paper)
        if domain:
            statement = statement.where(
                or_(in_domain, related_domain) if query.strip() else in_domain
            )
    else:
        if query.strip():
            statement = statement.where(matched)
        if domain:
            statement = statement.where(in_domain)
        if asset_type and asset_type != "all":
            statement = statement.where(Asset.asset_type == asset_type)
    if status:
        statement = statement.where(reading_status() == status)
    if author:
        authors = json_values(session, Asset.authors, "filter_authors")
        statement = statement.where(
            select(1)
            .select_from(authors)
            .where(func.lower(authors.c.value).contains(author.casefold(), autoescape=True))
            .exists()
        )
    if year_from is not None:
        statement = statement.where(Asset.year >= year_from)
    if year_to is not None:
        statement = statement.where(Asset.year <= year_to)
    if tags:
        values = json_values(session, Asset.tags, "filter_tags")
        statement = statement.where(
            select(1).select_from(values).where(values.c.value.in_(tags)).exists()
        )
    if has_annotation is not None:
        annotation_present = (
            select(1)
            .select_from(Annotation)
            .where(Annotation.asset_id == Asset.id, Annotation.deleted_at.is_(None))
            .exists()
        )
        statement = statement.where(annotation_present if has_annotation else ~annotation_present)
    for kind, requested in {
        "pdf": has_pdf,
        "html": has_html,
        "code": has_code,
        "guide": has_guide,
    }.items():
        if requested is None:
            continue
        present: Any = action_exists(kind)
        if grouped and kind == "code":
            related_code = (
                select(1)
                .select_from(links)
                .where(links.c.paper_id == Asset.id, action_exists("code", links.c.repository_id))
                .exists()
            )
            present = case((Asset.asset_type == "paper", related_code), else_=present)
        statement = statement.where(present if requested else ~present)
    if grouped and query.strip() and asset_type != "paper":
        # A matching repository is covered only by paper units that pass the same filters.
        # This also keeps a repository searchable when its paper is filtered out.
        qualified_papers = statement.where(Asset.asset_type == "paper").cte("qualified_papers")
        covered = (
            select(1)
            .select_from(links)
            .where(
                links.c.repository_id == Asset.id,
                links.c.paper_id.in_(select(qualified_papers.c.id)),
            )
            .exists()
        )
        statement = statement.where(or_(Asset.asset_type != "repository", ~covered))
    if date_from is not None or date_to is not None:
        day = publication_day(session)
        if date_from is not None:
            statement = statement.where(
                day >= (date_from if _postgres(session) else date_from.isoformat())
            )
        if date_to is not None:
            statement = statement.where(
                day <= (date_to if _postgres(session) else date_to.isoformat())
            )
    return statement


def catalog_facets(session: Session, selected: Any, memberships: Any) -> dict[str, dict[str, int]]:
    selected = selected.cte("facet_assets")
    assets = Asset.__table__.join(selected, Asset.id == selected.c.id)
    statements = []
    for name, column in [
        ("years", Asset.year),
        ("types", Asset.asset_type),
        ("reading_status", reading_status()),
    ]:
        statement = (
            select(
                literal(name).label("kind"),
                cast(column, String).label("key"),
                func.count().label("count"),
            )
            .select_from(assets)
            .group_by(column)
        )
        if name == "years":
            statement = statement.where(column.is_not(None))
        statements.append(statement)
    statements.append(
        select(literal("domains"), memberships.c.slug, func.count())
        .join(selected, selected.c.id == memberships.c.asset_id)
        .group_by(memberships.c.slug)
    )
    tags = json_values(session, Asset.tags, "facet_tags")
    statements.append(
        select(literal("tags"), tags.c.value, func.count())
        .select_from(assets)
        .join(tags, true())
        .group_by(tags.c.value)
    )
    buckets: dict[str, list[tuple[str, int]]] = {
        name: [] for name in ("years", "types", "reading_status", "domains", "tags")
    }
    for name, key, count in session.execute(union_all(*statements)):
        buckets[name].append((key, count))
    result = {}
    for name, values in buckets.items():
        ordered = (
            sorted(values, reverse=True)
            if name == "years"
            else sorted(values, key=lambda item: (-item[1], item[0]))
        )
        result[name] = dict(ordered[:50] if name == "tags" else ordered)
    return result


def sorted_selection(
    session: Session,
    selected: Any,
    *,
    sort: str,
    grouped: bool,
    query: str,
    hits: dict[UUID, Any],
    links: Any,
) -> Any:
    if sort == "relevance":
        scores = {asset_id: round(float(hit.score), 6) for asset_id, hit in hits.items()}
        score = case(scores, value=Asset.id, else_=1.0) if scores else literal(1.0)
        order = (
            case(
                {asset_id: index for index, asset_id in enumerate(hits)},
                value=Asset.id,
                else_=len(hits),
            )
            if hits
            else literal(0)
        )
        if grouped and hits:
            repo_score = (
                select(func.max(case(scores, value=links.c.repository_id, else_=0.0)))
                .where(links.c.paper_id == Asset.id)
                .scalar_subquery()
            )
            score = case((func.coalesce(repo_score, 0.0) > score, repo_score), else_=score)
            repo_order = (
                select(
                    func.min(
                        case(
                            {asset_id: index for index, asset_id in enumerate(hits)},
                            value=links.c.repository_id,
                            else_=len(hits),
                        )
                    )
                )
                .where(links.c.paper_id == Asset.id)
                .scalar_subquery()
            )
            order = case((func.coalesce(repo_order, len(hits)) < order, repo_order), else_=order)
        return (
            selected.order_by(score.desc(), order, Asset.updated_at.desc(), Asset.id)
            if grouped
            else selected.order_by(order, Asset.updated_at.desc(), Asset.id)
        )
    day = cast(publication_day(session), String)
    year = cast(Asset.year, String) + literal("-01-01")
    primary = func.coalesce(day, year, cast(Asset.created_at, String), "")
    fields: list[Any] = [primary, func.coalesce(Asset.year, 0), Asset.created_at]
    ordering = [field.desc() if sort == "date_desc" else field.asc() for field in fields]
    if grouped and query.strip():
        titles = [
            func.lower(func.trim(Asset.title)),
            func.lower(func.trim(func.coalesce(Asset.title_zh, ""))),
        ]
        normalized = " ".join(query.casefold().split())
        priority = case(
            (or_(*(title == normalized for title in titles)), 0),
            (or_(*(title.contains(normalized, autoescape=True) for title in titles)), 1),
            else_=2,
        )
        ordering.insert(0, priority)
    return selected.order_by(*ordering, Asset.id)


def load_catalog_assets(session: Session, ids: list[UUID]) -> list[Any]:
    if not ids:
        return []
    # A read projection cannot accidentally flush partial attributes back to the source asset.
    fields = [
        column
        for column in Asset.__table__.columns
        if column.name not in {"content", "analysis", "attributes"}
    ]
    attribute_keys = ("category", "first_public_date", "release_status")
    attributes = (
        func.jsonb_build_object(
            *(part for key in attribute_keys for part in (key, Asset.attributes[key]))
        )
        if _postgres(session)
        else func.json_object(
            *(
                part
                for key in attribute_keys
                for part in (key, func.json_extract(Asset.attributes, "$." + key))
            )
        )
    )
    assets = []
    for row in session.execute(
        select(*fields, attributes.label("attributes")).where(Asset.id.in_(ids))
    ).mappings():
        values = dict(row)
        if isinstance(values["attributes"], str):
            values["attributes"] = json.loads(values["attributes"])
        values["attributes"] = {
            key: value for key, value in values["attributes"].items() if value is not None
        }
        assets.append(SimpleNamespace(**values))
    annotate_presentation_domains(session, assets)
    by_id = {asset.id: asset for asset in assets}
    return [by_id[asset_id] for asset_id in ids]


def serialize_catalog_asset(
    asset: Asset,
    *,
    reading_state: ReadingState | None = None,
    annotation_summary: str | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    """Card metadata excludes full original and analysis bodies kept in the detail API."""
    fields = [
        "asset_type",
        "title",
        "title_zh",
        "subtitle",
        "authors",
        "year",
        "venue",
        "language",
        "abstract",
        "summary",
        "visibility",
        "source_urls",
        "tags",
        "row_version",
    ]
    result = {field: getattr(asset, field) for field in fields}
    result["attributes"] = {
        key: value
        for key, value in (asset.attributes or {}).items()
        if key in {"release_status", "first_public_date", "category"}
    }
    result.update(
        id=str(asset.id),
        type=asset.asset_type,
        domain_slugs=presentation_domains(asset),
        reading_status=reading_state.status if reading_state else "unread",
        reading_state={
            "status": reading_state.status,
            "updated_at": reading_state.updated_at.isoformat(),
        }
        if reading_state
        else {"status": "unread"},
        annotation_summary=annotation_summary,
    )
    result.update(
        {
            field: getattr(asset, field).isoformat() if getattr(asset, field) else None
            for field in ["created_at", "updated_at", "deleted_at"]
        }
    )
    if score is not None:
        result["score"] = score
    return result


def domain_counts(
    session: Session, settings: Any, *, include_overview: bool = False, slug: str | None = None
) -> list[dict[str, Any]]:
    from apps.api.routes_v1 import _asset_context, _domain_overviews
    from apps.api.serialization import serialize_domain_overview
    from packages.domain_catalog.service import list_domain_definitions

    definitions = list_domain_definitions(session, settings, include_hidden=True)
    known = {domain["slug"] for domain in definitions}
    catalog = [
        {key: value for key, value in domain.items() if key != "hidden_from_catalog"}
        for domain in definitions
        if not domain["hidden_from_catalog"]
    ]
    source_domains = json_values(session, Asset.domain_slugs, "registered_asset_domains")
    discovery = (
        select(source_domains.c.value)
        .select_from(Asset)
        .join(source_domains, true())
        .where(Asset.deleted_at.is_(None), source_domains.c.value.not_in(known))
    )
    if _postgres(session):
        # Most rows use only registered slugs; expand arrays only for unknown domains.
        discovery = discovery.where(~cast(Asset.domain_slugs, JSONB).contained_by(sorted(known)))
    discovered = set(session.scalars(discovery.distinct()))
    if discovered:
        # Known taxonomy slugs need no attributes read. Inspect visibility only for new slugs.
        discovered = set(
            session.scalars(
                select(source_domains.c.value)
                .select_from(Asset)
                .join(source_domains, true())
                .where(
                    Asset.deleted_at.is_(None),
                    source_domains.c.value.in_(discovered),
                    or_(
                        ~func.coalesce(hidden_clause(session), False),
                        and_(
                            Asset.asset_type == "collection",
                            Asset.attributes["kind"].as_string() == "domain-overview",
                        ),
                    ),
                )
                .distinct()
            )
        )
    catalog.extend(
        {"slug": value, "label": value, "aliases": [], "description": None, "tags": []}
        for value in sorted(discovered - known)
    )
    if slug is not None:
        catalog = [domain for domain in catalog if domain["slug"] == slug]
    if not catalog:
        return []
    memberships = domain_membership(session)
    annotations = (
        select(Annotation.asset_id, func.count().label("count"))
        .where(Annotation.deleted_at.is_(None))
        .group_by(Annotation.asset_id)
        .subquery()
    )
    states = (
        select(ReadingState.asset_id, ReadingState.status)
        .where(ReadingState.user_id == "local")
        .subquery()
    )
    linked_repositories = (
        select(Relation.object_asset_id)
        .where(
            Relation.relation_type == "implemented_by",
            Relation.confirmation_status == "confirmed",
            Relation.deleted_at.is_(None),
        )
        .distinct()
        .subquery()
    )
    ranked = (
        select(
            memberships.c.slug,
            Asset.id.label("asset_id"),
            Asset.asset_type,
            func.coalesce(states.c.status, "unread").label("reading_status"),
            func.coalesce(annotations.c.count, 0).label("annotation_count"),
            linked_repositories.c.object_asset_id.label("covered_repository_id"),
            func.row_number()
            .over(partition_by=memberships.c.slug, order_by=(Asset.updated_at.desc(), Asset.id))
            .label("rank"),
        )
        .join(Asset, Asset.id == memberships.c.asset_id)
        .outerjoin(annotations, annotations.c.asset_id == Asset.id)
        .outerjoin(states, states.c.asset_id == Asset.id)
        .outerjoin(linked_repositories, linked_repositories.c.object_asset_id == Asset.id)
        .where(
            visible_clause(session), memberships.c.slug.in_([domain["slug"] for domain in catalog])
        )
        .cte("ranked_domain_assets")
    )
    recent_pairs = (
        (
            func.jsonb_agg(func.jsonb_build_array(ranked.c.rank, cast(ranked.c.asset_id, String)))
            if _postgres(session)
            else func.json_group_array(
                func.json_array(ranked.c.rank, cast(ranked.c.asset_id, String))
            )
        )
        .filter(ranked.c.rank <= 5)
        .label("recent")
    )
    rows = session.execute(
        select(
            ranked.c.slug,
            func.count().label("assets"),
            func.sum(case((ranked.c.asset_type == "paper", 1), else_=0)).label("papers"),
            func.sum(case((ranked.c.asset_type == "repository", 1), else_=0)).label("repositories"),
            func.sum(
                case(
                    (
                        and_(
                            ranked.c.asset_type == "repository",
                            ranked.c.covered_repository_id.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("independent"),
            func.sum(ranked.c.annotation_count).label("annotations"),
            *(
                func.sum(case((ranked.c.reading_status == status, 1), else_=0)).label(status)
                for status in ("unread", "reading", "read")
            ),
            recent_pairs,
        ).group_by(ranked.c.slug)
    ).all()
    counts = {row.slug: row for row in rows}
    recent_ids = {
        row.slug: [
            UUID(asset_id)
            for _, asset_id in sorted(
                json.loads(row.recent) if isinstance(row.recent, str) else row.recent
            )
        ]
        for row in rows
    }
    recent = load_catalog_assets(
        session, list(dict.fromkeys(asset_id for ids in recent_ids.values() for asset_id in ids))
    )
    recent_by_id = {asset.id: asset for asset in recent}
    reading, summaries = _asset_context(session, recent)
    portals = list(
        session.scalars(
            select(Asset)
            .options(defer(Asset.content, raiseload=True), defer(Asset.analysis, raiseload=True))
            .where(
                Asset.deleted_at.is_(None),
                Asset.asset_type == "collection",
                hidden_clause(session),
                Asset.attributes["kind"].as_string() == "domain-overview",
            )
        )
    )
    overviews = _domain_overviews(session, portals)
    unavailable = serialize_domain_overview(
        asset_id=None,
        representation_id=None,
        status="unavailable",
        default_presentation="original",
        last_verified_at=None,
        warning="No domain overview is registered.",
    )
    result = []
    for domain in catalog:
        count = counts.get(domain["slug"])
        overview = overviews.get(domain["slug"], unavailable)
        item = {
            **domain,
            "name": domain["label"],
            "asset_count": count.assets if count else 0,
            "paper_count": count.papers if count else 0,
            "repository_count": count.repositories if count else 0,
            "research_unit_count": count.papers + count.independent if count else 0,
            "annotation_count": count.annotations if count else 0,
            "reading_counts": {
                status: getattr(count, status) if count else 0
                for status in ("unread", "reading", "read")
            },
            "overview_status": overview["status"],
            "recent_assets": [
                serialize_catalog_asset(
                    recent_by_id[asset_id],
                    reading_state=reading.get(asset_id),
                    annotation_summary=summaries.get(asset_id),
                )
                for asset_id in recent_ids.get(domain["slug"], [])
            ],
        }
        if include_overview:
            item["overview"] = overview
        result.append(item)
    return result
