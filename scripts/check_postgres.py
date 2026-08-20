import psycopg

conn = psycopg.connect(
    host="localhost",
    port=5432,
    dbname="amr_agent",
    user="amr",
    password="123456",
)

with conn.cursor() as cur:
    cur.execute("SELECT 1;")
    print(cur.fetchone())

conn.close()