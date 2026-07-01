/* ============================================================================
   TEST DATA CLEANUP — run AFTER you've tested both fixes.
   Run against the same database as test_data_setup.sql.
   ============================================================================ */
SET NOCOUNT ON;

-- Re-enable the background ghost-cleanup task (it was suppressed for the test).
DBCC TRACEOFF (661, -1);

IF OBJECT_ID('dbo.UnusedIndexTest') IS NOT NULL DROP TABLE dbo.UnusedIndexTest;
IF OBJECT_ID('dbo.GhostTest')       IS NOT NULL DROP TABLE dbo.GhostTest;

PRINT 'Test tables dropped and trace flag 661 disabled.';
