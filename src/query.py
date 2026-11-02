"""Run a .sql file against the warehouse and print each result table."""
import sys
import duckdb
import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)

con = duckdb.connect("data/warehouse/fleetpulse.duckdb")
text = open(sys.argv[1], encoding="utf-8").read()

for stmt in text.split(";"):
    stmt = stmt.strip()
    if not stmt or all(l.strip().startswith("--") for l in stmt.splitlines()):
        continue
    title = stmt.splitlines()[0]
    # Ignore comment lines when deciding whether this is a CREATE statement
    body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--"))
    if body.strip().upper().startswith("CREATE"):
        con.execute(stmt)
        continue
    print(f"\n{title}\n" + "-" * 80)
    print(con.execute(stmt).df().to_string(index=False))

con.close()