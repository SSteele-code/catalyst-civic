import os
import psycopg2

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

def run_schema_check():
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()

        tables = [
            ("m1_agenda", "pipeline_ledger"),
            ("m1_agenda", "meetings"),
            ("m1_agenda", "items"),
            ("cco", "registry"),
            ("cco", "observations")
        ]

        for schema, table in tables:
            print(f"\n--- Columns in {schema}.{table} ---")
            cur.execute(f"""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_schema = '{schema}' AND table_name = '{table}'
                ORDER BY ordinal_position;
            """)
            for col in cur.fetchall():
                print(f"{col[0]} ({col[1]})")

        cur.close()
        conn.close()
    except Exception as e:
        print(f"SCHEMA CHECK FAILED: {e}")

if __name__ == "__main__":
    run_schema_check()
