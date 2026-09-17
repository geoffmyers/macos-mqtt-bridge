-- src/macos_bridge/queries/daily_total.sql
-- Returns one row: total_seconds (FLOAT) of foreground app usage today.
SELECT COALESCE(SUM(ZENDDATE - ZSTARTDATE), 0.0) AS total_seconds
FROM ZOBJECT
WHERE ZSTREAMNAME = '/app/usage'
  AND ZSTARTDATE >= :since_mat;
