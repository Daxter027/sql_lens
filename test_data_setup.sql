/* ============================================================================
   TEST DATA SETUP — exercise Issue 4 (unused index) and Issue 5 (ghost pages)
   in the SQL Storage Optimizer.

   HOW TO USE
   1. Run this against the SAME database you connect the tool to (e.g. latest4).
   2. In the tool, click "Re-run Analysis".
   3. The "Unused Index" and "Ghost Page" cards will now show a candidate.
   4. Select both and run the optimizations, then watch the before/after.
   5. When done, run test_data_cleanup.sql.

   Requires sysadmin (the ghost test uses an instance-wide trace flag).
   ============================================================================ */
SET NOCOUNT ON;

/* ---- ISSUE 4: high-overhead UNUSED INDEX ----------------------------------
   A non-clustered, non-unique index that receives many WRITES but zero READS.
   Detector threshold: user_updates >= 500 AND reads = 0. We do 700 inserts. */
IF OBJECT_ID('dbo.UnusedIndexTest') IS NOT NULL DROP TABLE dbo.UnusedIndexTest;
CREATE TABLE dbo.UnusedIndexTest (
    id     INT IDENTITY(1,1) PRIMARY KEY,   -- clustered PK (index_id 1, ignored)
    label  NVARCHAR(50),
    filler INT
);
-- This non-clustered, non-unique index is the "unused" candidate (index_id 2).
-- IMPORTANT: index the INT column `filler`, NOT the string column `label`.
-- The string_storage check scans `label`, and if the index covered `label` the
-- optimizer would use it for that scan, registering a read and disqualifying it
-- as "unused". Indexing an INT column keeps reads at 0.
CREATE NONCLUSTERED INDEX IX_UnusedIndexTest_filler ON dbo.UnusedIndexTest(filler);

DECLARE @i INT = 0;
WHILE @i < 700                 -- 700 inserts => ~700 user_updates on the index
BEGIN
    INSERT dbo.UnusedIndexTest(label, filler) VALUES (N'row' + CAST(@i AS NVARCHAR(10)), @i);
    SET @i += 1;
END
-- NOTE: do NOT run "SELECT ... WHERE label = ..." against this table, or the
-- index gains a read and no longer qualifies as unused.

/* ---- ISSUE 5: GHOST records -----------------------------------------------
   Ghost records = rows deleted but not yet removed by the background cleanup
   task. Detector threshold: ghost_record_count >= 1000.
   Trace flag 661 suppresses the background ghost-cleanup task so the ghosts
   persist long enough to test. It is INSTANCE-WIDE — the cleanup script turns
   it back off. */
DBCC TRACEON (661, -1);

IF OBJECT_ID('dbo.GhostTest') IS NOT NULL DROP TABLE dbo.GhostTest;
CREATE TABLE dbo.GhostTest (
    id      INT IDENTITY(1,1) PRIMARY KEY,
    payload CHAR(200) NOT NULL DEFAULT('x')
);
INSERT dbo.GhostTest(payload)
SELECT TOP (6000) 'x'
FROM sys.all_objects a CROSS JOIN sys.all_objects b;

DELETE FROM dbo.GhostTest WHERE id % 6 <> 0;   -- deletes ~5000 rows => ~5000 ghosts

/* ---- VERIFY what the tool should now detect -------------------------------- */
SELECT 'unused index' AS check_name,
       i.name         AS index_name,
       us.user_updates                                        AS writes,
       (us.user_seeks + us.user_scans + us.user_lookups)      AS reads
FROM sys.indexes i
JOIN sys.dm_db_index_usage_stats us
     ON us.object_id = i.object_id AND us.index_id = i.index_id AND us.database_id = DB_ID()
WHERE i.object_id = OBJECT_ID('dbo.UnusedIndexTest') AND i.index_id > 1;

SELECT 'ghost records' AS check_name,
       OBJECT_NAME(ips.object_id) AS table_name,
       ips.ghost_record_count
-- SAMPLED, not LIMITED: LIMITED returns NULL for ghost_record_count (it only
-- reads non-leaf pages), so it would show nothing even when ghosts exist.
FROM sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID('dbo.GhostTest'), NULL, NULL, 'SAMPLED') ips
WHERE ips.ghost_record_count > 0;
