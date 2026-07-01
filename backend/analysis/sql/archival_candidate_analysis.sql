-- ============================================================
-- LEGACY DATABASE ARCHIVAL CANDIDATE ANALYSIS SCRIPT
-- SQL Server 2008 Compatible
-- ============================================================

SET NOCOUNT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;

-- ============================================================
-- SECTION 1: CREATE TEMPORARY TABLES
-- ============================================================

IF OBJECT_ID('tempdb..#CandidateTables') IS NOT NULL DROP TABLE #CandidateTables;
IF OBJECT_ID('tempdb..#DateAnalysis')     IS NOT NULL DROP TABLE #DateAnalysis;
IF OBJECT_ID('tempdb..#StorageAnalysis')  IS NOT NULL DROP TABLE #StorageAnalysis;
IF OBJECT_ID('tempdb..#FailedTables')     IS NOT NULL DROP TABLE #FailedTables;
IF OBJECT_ID('tempdb..#FinalReport')      IS NOT NULL DROP TABLE #FinalReport;
IF OBJECT_ID('tempdb..#SpaceUsed')        IS NOT NULL DROP TABLE #SpaceUsed;

-- COLLATE DATABASE_DEFAULT on name columns: temp tables otherwise inherit
-- tempdb's collation, which can differ from the user database's collation and
-- breaks equality joins against sys.schemas/sys.tables (collation conflict).
CREATE TABLE #CandidateTables (
    SchemaName  NVARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL,
    TableName   NVARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL
);

CREATE TABLE #DateAnalysis (
    SchemaName          NVARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL,
    TableName           NVARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL,
    EarliestDate        DATETIME      NULL,
    LatestDate          DATETIME      NULL
);

CREATE TABLE #StorageAnalysis (
    SchemaName  NVARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL,
    TableName   NVARCHAR(128) COLLATE DATABASE_DEFAULT NOT NULL,
    TotalRows   BIGINT        NULL,
    ReservedMB  DECIMAL(18,2) NULL,
    DataMB      DECIMAL(18,2) NULL,
    IndexMB     DECIMAL(18,2) NULL,
    UnusedMB    DECIMAL(18,2) NULL
);

CREATE TABLE #FailedTables (
    SchemaName    NVARCHAR(128)  COLLATE DATABASE_DEFAULT NOT NULL,
    TableName     NVARCHAR(128)  COLLATE DATABASE_DEFAULT NOT NULL,
    ErrorMessage  NVARCHAR(4000) NOT NULL
);

CREATE TABLE #FinalReport (
    SchemaName           NVARCHAR(128)  COLLATE DATABASE_DEFAULT NULL,
    TableName            NVARCHAR(128)  COLLATE DATABASE_DEFAULT NULL,
    OverallEarliestDate  DATETIME       NULL,
    OverallLatestDate    DATETIME       NULL,
    YearsSinceLatestDate DECIMAL(10,2)  NULL,
    TotalRows            BIGINT         NULL,
    ReservedMB           DECIMAL(18,2)  NULL,
    DataMB               DECIMAL(18,2)  NULL,
    IndexMB              DECIMAL(18,2)  NULL,
    UnusedMB             DECIMAL(18,2)  NULL
);

CREATE TABLE #SpaceUsed (
    [name]      NVARCHAR(128) NULL,
    [rows]      NVARCHAR(50)  NULL,
    reserved    NVARCHAR(50)  NULL,
    data        NVARCHAR(50)  NULL,
    index_size  NVARCHAR(50)  NULL,
    unused      NVARCHAR(50)  NULL
);

-- ============================================================
-- SECTION 2: IDENTIFY CANDIDATE TABLES
-- Criteria: No FK (parent or child), no SP/View/Function refs,
--           no triggers
-- ============================================================

-- All user tables
INSERT INTO #CandidateTables (SchemaName, TableName)
SELECT
    s.name,
    t.name
FROM sys.tables  t
INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
WHERE t.is_ms_shipped = 0;

-- Remove tables that are the CHILD side of any FK
DELETE ct
FROM #CandidateTables ct
WHERE EXISTS (
    SELECT 1
    FROM sys.foreign_keys    fk
    INNER JOIN sys.tables    tp ON fk.parent_object_id = tp.object_id
    INNER JOIN sys.schemas   sp ON tp.schema_id        = sp.schema_id
    WHERE sp.name = ct.SchemaName
      AND tp.name = ct.TableName
);

-- Remove tables that are the PARENT (referenced) side of any FK
DELETE ct
FROM #CandidateTables ct
WHERE EXISTS (
    SELECT 1
    FROM sys.foreign_key_columns fkc
    INNER JOIN sys.tables        tr ON fkc.referenced_object_id = tr.object_id
    INNER JOIN sys.schemas       sr ON tr.schema_id             = sr.schema_id
    WHERE sr.name = ct.SchemaName
      AND tr.name = ct.TableName
);

-- Remove tables referenced by stored procedures, functions, or views
DELETE ct
FROM #CandidateTables ct
WHERE EXISTS (
    SELECT 1
    FROM sys.sql_expression_dependencies dep
    INNER JOIN sys.objects               obj ON dep.referencing_id = obj.object_id
    INNER JOIN sys.tables                trg ON dep.referenced_id  = trg.object_id
    INNER JOIN sys.schemas               srg ON trg.schema_id      = srg.schema_id
    WHERE obj.type IN ('P','FN','IF','TF','V')
      AND srg.name = ct.SchemaName
      AND trg.name = ct.TableName
);

-- Remove tables that have triggers
DELETE ct
FROM #CandidateTables ct
WHERE EXISTS (
    SELECT 1
    FROM sys.triggers  tr
    INNER JOIN sys.tables  tb ON tr.parent_id = tb.object_id
    INNER JOIN sys.schemas sb ON tb.schema_id = sb.schema_id
    WHERE sb.name = ct.SchemaName
      AND tb.name = ct.TableName
);

-- ============================================================
-- SECTION 3: DATE ANALYSIS VIA DYNAMIC SQL
-- For each candidate table find the overall MIN and MAX
-- across all datetime-family columns using UNION ALL subqueries.
-- All dynamic SQL writes results through sp_executesql parameters
-- into local variables, then inserts into #DateAnalysis here,
-- so no column names from #DateAnalysis appear inside the
-- dynamically built string.
-- ============================================================

DECLARE @SchemaName      NVARCHAR(128);
DECLARE @TableName       NVARCHAR(128);
DECLARE @SQL             NVARCHAR(MAX);
DECLARE @UnionMinParts   NVARCHAR(MAX);
DECLARE @UnionMaxParts   NVARCHAR(MAX);
DECLARE @EarliestDate    DATETIME;
DECLARE @LatestDate      DATETIME;
DECLARE @ErrMsg          NVARCHAR(4000);

DECLARE cur_dates CURSOR LOCAL FAST_FORWARD FOR
    SELECT SchemaName, TableName
    FROM   #CandidateTables;

OPEN cur_dates;
FETCH NEXT FROM cur_dates INTO @SchemaName, @TableName;

WHILE @@FETCH_STATUS = 0
BEGIN
    -- Reset
    SET @UnionMinParts = NULL;
    SET @UnionMaxParts = NULL;
    SET @EarliestDate  = NULL;
    SET @LatestDate    = NULL;

    -- Build one UNION ALL branch per datetime-family column
    SELECT
        @UnionMinParts = ISNULL(@UnionMinParts + N' UNION ALL ', N'') +
            N'SELECT MIN(CAST(' + QUOTENAME(c.name) + N' AS DATETIME)) FROM ' +
            QUOTENAME(s.name) + N'.' + QUOTENAME(t.name),
        @UnionMaxParts = ISNULL(@UnionMaxParts + N' UNION ALL ', N'') +
            N'SELECT MAX(CAST(' + QUOTENAME(c.name) + N' AS DATETIME)) FROM ' +
            QUOTENAME(s.name) + N'.' + QUOTENAME(t.name)
    FROM sys.columns c
    INNER JOIN sys.types   ty ON c.user_type_id = ty.user_type_id
    INNER JOIN sys.tables   t ON c.object_id    = t.object_id
    INNER JOIN sys.schemas  s ON t.schema_id    = s.schema_id
    WHERE s.name  = @SchemaName
      AND t.name  = @TableName
      AND ty.name IN ('datetime','smalldatetime','date','datetime2');

    IF @UnionMinParts IS NULL
    BEGIN
        -- Table has no datetime columns; store NULLs
        INSERT INTO #DateAnalysis (SchemaName, TableName, EarliestDate, LatestDate)
        VALUES (@SchemaName, @TableName, NULL, NULL);
    END
    ELSE
    BEGIN
        -- Build a scalar SELECT that returns the overall min and max
        -- into output parameters; no reference to #DateAnalysis columns
        -- appears inside the string itself.
        SET @SQL =
            N'SELECT @pMin = MIN(d), @pMax = MAX(d) FROM (' +
            N'    SELECT d FROM (' + @UnionMinParts + N') AS _mn(d)' +
            N'    UNION ALL' +
            N'    SELECT d FROM (' + @UnionMaxParts + N') AS _mx(d)' +
            N') AS _combined(d)';

        BEGIN TRY
            EXEC sp_executesql
                @SQL,
                N'@pMin DATETIME OUTPUT, @pMax DATETIME OUTPUT',
                @pMin = @EarliestDate OUTPUT,
                @pMax = @LatestDate   OUTPUT;

            INSERT INTO #DateAnalysis (SchemaName, TableName, EarliestDate, LatestDate)
            VALUES (@SchemaName, @TableName, @EarliestDate, @LatestDate);
        END TRY
        BEGIN CATCH
            SET @ErrMsg = ERROR_MESSAGE();
            INSERT INTO #FailedTables (SchemaName, TableName, ErrorMessage)
            VALUES (@SchemaName, @TableName, N'DateAnalysis: ' + @ErrMsg);

            -- Placeholder so the table still appears in the final report
            INSERT INTO #DateAnalysis (SchemaName, TableName, EarliestDate, LatestDate)
            VALUES (@SchemaName, @TableName, NULL, NULL);
        END CATCH
    END

    FETCH NEXT FROM cur_dates INTO @SchemaName, @TableName;
END

CLOSE cur_dates;
DEALLOCATE cur_dates;

-- ============================================================
-- SECTION 4: STORAGE ANALYSIS VIA sp_spaceused
-- ============================================================

DECLARE @FullTableName NVARCHAR(300);

DECLARE cur_storage CURSOR LOCAL FAST_FORWARD FOR
    SELECT SchemaName, TableName
    FROM   #CandidateTables;

OPEN cur_storage;
FETCH NEXT FROM cur_storage INTO @SchemaName, @TableName;

WHILE @@FETCH_STATUS = 0
BEGIN
    SET @FullTableName = QUOTENAME(@SchemaName) + N'.' + QUOTENAME(@TableName);

    BEGIN TRY
        DELETE FROM #SpaceUsed;

        INSERT INTO #SpaceUsed ([name],[rows],reserved,data,index_size,unused)
        EXEC sp_spaceused @FullTableName;

        INSERT INTO #StorageAnalysis
            (SchemaName, TableName, TotalRows, ReservedMB, DataMB, IndexMB, UnusedMB)
        SELECT
            @SchemaName,
            @TableName,
            CAST(REPLACE([rows],     ' ', '') AS BIGINT),
            CAST(REPLACE(reserved,   ' KB', '') AS DECIMAL(18,2)) / 1024.0,
            CAST(REPLACE(data,       ' KB', '') AS DECIMAL(18,2)) / 1024.0,
            CAST(REPLACE(index_size, ' KB', '') AS DECIMAL(18,2)) / 1024.0,
            CAST(REPLACE(unused,     ' KB', '') AS DECIMAL(18,2)) / 1024.0
        FROM #SpaceUsed;

    END TRY
    BEGIN CATCH
        SET @ErrMsg = ERROR_MESSAGE();
        INSERT INTO #FailedTables (SchemaName, TableName, ErrorMessage)
        VALUES (@SchemaName, @TableName, N'StorageAnalysis: ' + @ErrMsg);

        IF NOT EXISTS (
            SELECT 1 FROM #StorageAnalysis
            WHERE SchemaName = @SchemaName AND TableName = @TableName
        )
        BEGIN
            INSERT INTO #StorageAnalysis
                (SchemaName, TableName, TotalRows, ReservedMB, DataMB, IndexMB, UnusedMB)
            VALUES (@SchemaName, @TableName, 0, 0, 0, 0, 0);
        END
    END CATCH

    FETCH NEXT FROM cur_storage INTO @SchemaName, @TableName;
END

CLOSE cur_storage;
DEALLOCATE cur_storage;

-- ============================================================
-- SECTION 5: ASSEMBLE FINAL REPORT
-- Column aliases from #DateAnalysis (EarliestDate / LatestDate)
-- are renamed to the required output names here only, inside
-- a static INSERT where the compiler can resolve them correctly.
-- ============================================================

INSERT INTO #FinalReport
    (SchemaName, TableName, OverallEarliestDate, OverallLatestDate,
     YearsSinceLatestDate, TotalRows, ReservedMB, DataMB, IndexMB, UnusedMB)
SELECT
    ct.SchemaName,
    ct.TableName,
    da.EarliestDate,
    da.LatestDate,
    CASE
        WHEN da.LatestDate IS NOT NULL
        THEN CAST(DATEDIFF(DAY, da.LatestDate, GETDATE()) / 365.25 AS DECIMAL(10,2))
        ELSE NULL
    END,
    ISNULL(sa.TotalRows,  0),
    ISNULL(sa.ReservedMB, 0),
    ISNULL(sa.DataMB,     0),
    ISNULL(sa.IndexMB,    0),
    ISNULL(sa.UnusedMB,   0)
FROM #CandidateTables  ct
LEFT JOIN #DateAnalysis    da ON da.SchemaName = ct.SchemaName AND da.TableName = ct.TableName
LEFT JOIN #StorageAnalysis sa ON sa.SchemaName = ct.SchemaName AND sa.TableName = ct.TableName;

-- ============================================================
-- SECTION 6: FINAL OUTPUT
-- ============================================================

SELECT
    SchemaName,
    TableName,
    OverallEarliestDate,
    OverallLatestDate,
    YearsSinceLatestDate,
    TotalRows,
    ReservedMB,
    DataMB,
    IndexMB,
    UnusedMB
FROM #FinalReport
ORDER BY
    OverallLatestDate ASC,
    ReservedMB        DESC;

-- ============================================================
-- SECTION 7: FAILED TABLES REPORT
-- ============================================================

IF EXISTS (SELECT 1 FROM #FailedTables)
BEGIN
    SELECT SchemaName, TableName, ErrorMessage
    FROM   #FailedTables
    ORDER BY SchemaName, TableName;
END
ELSE
BEGIN
    SELECT 'No tables encountered errors during analysis.' AS FailedTablesStatus;
END

-- ============================================================
-- CLEANUP
-- ============================================================

IF OBJECT_ID('tempdb..#CandidateTables') IS NOT NULL DROP TABLE #CandidateTables;
IF OBJECT_ID('tempdb..#DateAnalysis')     IS NOT NULL DROP TABLE #DateAnalysis;
IF OBJECT_ID('tempdb..#StorageAnalysis')  IS NOT NULL DROP TABLE #StorageAnalysis;
IF OBJECT_ID('tempdb..#SpaceUsed')        IS NOT NULL DROP TABLE #SpaceUsed;
IF OBJECT_ID('tempdb..#FinalReport')      IS NOT NULL DROP TABLE #FinalReport;
IF OBJECT_ID('tempdb..#FailedTables')     IS NOT NULL DROP TABLE #FailedTables;
