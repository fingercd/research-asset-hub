# Database migrations

Alembic uses `packages.domain.models.Base.metadata` as the authoritative
operational schema. The initial revision creates only the hub's PostgreSQL
tables; it never reads, moves, or deletes files in any registered source.

Upgrade from an empty database:

```powershell
alembic -c migrations/alembic.ini upgrade head
```

Downgrading the initial revision drops the hub's operational tables and must
only be done after a database dump plus an open catalog export. Source folders
remain untouched, but reading state and annotations would otherwise be lost.
