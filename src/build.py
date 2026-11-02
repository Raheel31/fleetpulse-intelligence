"""One-command refresh: raw CSVs -> warehouse -> marts and investigation views."""
from pathlib import Path
import duckdb
from src import etl, generate_data

DB = "data/warehouse/fleetpulse.duckdb"
VIEW_FILES = ["sql/02_marts.sql", "sql/03_decline_investigation.sql"]


def create_views():
    con = duckdb.connect(DB)
    for f in VIEW_FILES:
        for stmt in Path(f).read_text(encoding="utf-8").split(";"):
            body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
            if body.upper().startswith("CREATE"):
                con.execute(body)
    con.close()


def main():
    if not Path("data/raw/customers.csv").exists():
        generate_data.main()
    etl.build()
    create_views()


if __name__ == "__main__":
    main()