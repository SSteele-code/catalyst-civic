import os
import psycopg2
import sys
import json

PG_DB = "catalyst_civic"
PG_USER = "postgres"
PG_PASS = "postgres"

def check_registry(pulse_id):
    print(f"--- REGISTRY AUDIT: PULSE {pulse_id} ---")
    try:
        conn = psycopg2.connect(host="localhost", database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()
        
        # 1. Count Observations for this pulse
        cur.execute("SELECT registry_id, fact_key, evidence FROM cco.observations WHERE source_id LIKE %s", (f"{pulse_id}%",))
        obs = cur.fetchall()
        
        print(f"Entities Mentions Detected: {len(obs)}")
        
        # 2. Check for noise (Overly long registry IDs)
        noise = [o for o in obs if len(str(o[0])) > 50]
        if noise:
            print(f"WARNING: Potential Noise Detected ({len(noise)} long IDs):")
            for n in noise[:3]:
                print(f"  - {n[0]}")
                
        # 3. List legitimate people found
        cur.execute("""
            SELECT r.canonical_name 
            FROM cco.registry r
            JOIN cco.observations o ON o.registry_id = r.registry_id
            WHERE o.source_id LIKE %s AND r.category = 'PEOPLE'
        """, (f"{pulse_id}%",))
        people = cur.fetchall()
        if people:
            print("People Identified:")
            for p in people:
                print(f"  - {p[0]}")
        else:
            print("No people identified in this record.")

        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Registry Audit Error: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        success = check_registry(sys.argv[1])
        sys.exit(0 if success else 1)
