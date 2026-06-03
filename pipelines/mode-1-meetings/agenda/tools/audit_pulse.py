import os
import psycopg2
import sys
import json
from pathlib import Path

PG_DB = "catalyst_civic"
PG_USER = "postgres"
PG_PASS = "postgres"

def audit_pulse(pulse_id):
    print(f"--- DEEP DIVE: AUDITING PULSE {pulse_id} ---")
    try:
        conn = psycopg2.connect(host="localhost", database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()
        
        # 1. Check Meeting Metadata
        cur.execute("SELECT meeting_date, meeting_type, location FROM m1_agenda.meetings WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        meeting = cur.fetchone()
        if not meeting:
            print(f"FAILED: No meeting found for {pulse_id}")
            return False
            
        print(f"Meeting: {meeting[1]} | Date: {meeting[0]} | Location: {meeting[2]}")
        
        # 2. Check Items Structural Integrity
        cur.execute("SELECT COUNT(*) FROM m1_agenda.items WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        count = cur.fetchone()[0]
        
        if count == 0:
            print("FAILED: 0 items ingested.")
            return False
        
        # Check for empty titles
        cur.execute("SELECT COUNT(*) FROM m1_agenda.items WHERE meeting_id LIKE %s AND (title IS NULL OR title = '')", (f"{pulse_id}%",))
        empty_titles = cur.fetchone()[0]
        
        # Check for duplicates (common in multi-line failures)
        cur.execute("""
            SELECT title, COUNT(*) 
            FROM m1_agenda.items 
            WHERE meeting_id LIKE %s 
            GROUP BY title 
            HAVING COUNT(*) > 1
        """, (f"{pulse_id}%",))
        duplicates = cur.fetchall()
        
        print(f"Items: {count} total | Empty Titles: {empty_titles} | Duplicate Clusters: {len(duplicates)}")
        
        if empty_titles > (count * 0.2):
            print("WARNING: High number of empty titles.")
            
        if len(duplicates) > 3:
            print("FAILED: Too many duplicate items detected. Extraction likely unstable.")
            return False

        # 3. Check for specific sections (Town Council meetings should have these)
        if "COUNCIL" in str(meeting[1]).upper():
            required = ["MINUTES", "REPORTS", "ADJOURN"]
            cur.execute("SELECT title FROM m1_agenda.items WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
            titles = [str(r[0]).upper() for r in cur.fetchall()]
            missing = [r for r in required if not any(r in t for t in titles)]
            if missing:
                print(f"WARNING: Expected sections missing: {missing}")

        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Audit Error: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        success = audit_pulse(sys.argv[1])
        sys.exit(0 if success else 1)
