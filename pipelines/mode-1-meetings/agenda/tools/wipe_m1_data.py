import os
import psycopg2

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

def wipe_m1():
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()

        print("--- WIPING M1_AGENDA SCHEMA ---")
        cur.execute("TRUNCATE TABLE m1_agenda.items RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE m1_agenda.meetings RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE m1_agenda.pipeline_ledger RESTART IDENTITY CASCADE;")
        
        print("--- CLEANING CCO LINKS & REGISTRY ---")
        cur.execute("TRUNCATE TABLE cco.observations RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE cco.identities RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE cco.registry RESTART IDENTITY CASCADE;")
        
        conn.commit()
        print("\nSUCCESS: Mode 1 (Meetings) data and CCO Registry have been completely wiped.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"WIPE FAILED: {e}")

if __name__ == "__main__":
    wipe_m1()
