import pyodbc

conn = pyodbc.connect(
    "DRIVER={SQL Server};"
    "SERVER=localhost;"
    "DATABASE=welingkarlivelatest;"
    "Trusted_Connection=yes;"
)

print("Connected!")

cursor = conn.cursor()

cursor.execute("SELECT DB_NAME()")

print(cursor.fetchone()[0])