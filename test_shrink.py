import pyodbc

conn = pyodbc.connect('Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=Welingkarlive;Trusted_Connection=yes;')
cursor = conn.cursor()
conn.autocommit = True
shrink_sql = '''
    DECLARE @sql NVARCHAR(MAX);
    SET @sql = N'DBCC SHRINKFILE(' + QUOTENAME(?) + N', ' + CAST(? AS NVARCHAR(10)) + N') WITH NO_INFOMSGS';
    EXEC sp_executesql @sql;
'''
cursor.execute(shrink_sql, 'Welingkarlive_Log', 512)
print('SUCCESS!')
conn.close()
