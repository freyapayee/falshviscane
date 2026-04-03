from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import db


SQLITE_URL = "sqlite:///instance/viscane.db"
POSTGRES_URL = "postgresql://user:password@localhost:5432/viscane_db"

sqlite_engine = create_engine(SQLITE_URL)
postgres_engine = create_engine(POSTGRES_URL)

sqlite_meta = MetaData()
sqlite_meta.reflect(bind=sqlite_engine)


def create_postgres_tables():
    db.metadata.create_all(postgres_engine)


def get_primary_keys(engine, table_name):
    return inspect(engine).get_pk_constraint(table_name).get("constrained_columns", [])


def reset_postgres_sequence(connection, table_name):
    connection.execute(
        text(
            """
            SELECT setval(
                pg_get_serial_sequence(:table_name, 'id'),
                COALESCE((SELECT MAX(id) FROM "{table_name}"), 1),
                COALESCE((SELECT MAX(id) FROM "{table_name}") IS NOT NULL, false)
            )
            """.replace("{table_name}", table_name)
        ),
        {"table_name": table_name},
    )


def migrate_table(table_name):
    source_table = sqlite_meta.tables[table_name]
    target_table = Table(table_name, MetaData(), autoload_with=postgres_engine)
    primary_keys = get_primary_keys(postgres_engine, table_name)

    with sqlite_engine.connect() as sqlite_conn:
        rows = sqlite_conn.execute(select(source_table)).fetchall()

    if not rows:
        print(f"- {table_name}: no rows to migrate")
        return

    records = [dict(row._mapping) for row in rows]

    with postgres_engine.begin() as pg_conn:
        if primary_keys:
            statement = pg_insert(target_table).values(records)
            update_columns = {
                column.name: statement.excluded[column.name]
                for column in target_table.columns
                if column.name not in primary_keys
            }
            if update_columns:
                statement = statement.on_conflict_do_update(
                    index_elements=primary_keys,
                    set_=update_columns,
                )
            else:
                statement = statement.on_conflict_do_nothing(index_elements=primary_keys)
            result = pg_conn.execute(statement)
        else:
            result = pg_conn.execute(target_table.insert(), records)

        if "id" in target_table.c:
            reset_postgres_sequence(pg_conn, table_name)

    inserted = result.rowcount if result.rowcount is not None else len(records)
    print(f"- {table_name}: migrated {inserted} row(s)")


def migrate():
    print("Starting migration from SQLite to PostgreSQL...")
    create_postgres_tables()

    for table_name in sqlite_meta.sorted_tables:
        migrate_table(table_name.name)

    print("Migration complete.")


if __name__ == "__main__":
    migrate()
