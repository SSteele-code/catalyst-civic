import psycopg2
import os

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

def audit():
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()
        
        cur.execute("SELECT category, canonical_name, registry_id FROM cco.registry ORDER BY category, canonical_name")
        rows = cur.fetchall()
        
        print(f"{'CATEGORY':<15} | {'NAME':<40} | {'REGISTRY_ID'}")
        print("-" * 90)
        for r in rows:
            print(f"{r[0]:<15} | {r[1]:<40} | {r[2]}")
            
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    audit()
