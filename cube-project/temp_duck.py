import duckdb
con = duckdb.connect("cube-project/data/tutorial/tutorial.db")

print("Connected to DB.")
print("Type SQL commands. Type .quit to exit.")

while True:
    cmd = input("SQL> ")
    if cmd.strip().lower() in [".quit", "quit", "exit"]:
        break
    try:
        result = con.execute(cmd).fetchall()
        print(result)
    except Exception as e:
        print("Error:", e)
