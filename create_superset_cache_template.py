import requests
import psycopg2

CUBE_URL = "http://host.docker.internal:34000/cubejs-api/v1/load"

QUERY = {
    # ...YOUR_QUERY...
}

TABLE = "cube_cache.table_name"
INSERT_SQL = "INSERT INTO cube_cache.table_name (col1, col2) VALUES (%s, %s)"

def main():
    resp = requests.post(CUBE_URL, json={"query": QUERY}, timeout=60)
    print("Cube status:", resp.status_code)
    if resp.status_code != 200:
        print(resp.text)
    resp.raise_for_status()

    data = resp.json().get("data", [])
    print("rows from cube:", len(data))

    rows = [
        # (d["..."], d["..."])
        for d in data
    ]

    conn = psycopg2.connect(
        host="superset_db",
        port=5432,
        dbname="superset",
        user="superset",
        password="superset",
    )
    cur = conn.cursor()
    cur.execute(f"TRUNCATE {TABLE};")
    cur.executemany(INSERT_SQL, rows)
    conn.commit()
    cur.close()
    conn.close()
    print("loaded OK")

if __name__ == "__main__":
    main()
