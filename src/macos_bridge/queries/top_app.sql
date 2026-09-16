-- src/screen_time_bridge/queries/phase_a_top_app.sql
-- Returns at most one row: (bundle_id, total_seconds) for the most-used app today.
SELECT
    ZVALUESTRING AS bundle_id,
    SUM(ZENDDATE - ZSTARTDATE) AS total_seconds
FROM ZOBJECT
WHERE ZSTREAMNAME = '/app/usage'
  AND ZSTARTDATE >= :since_mat
  AND ZVALUESTRING IS NOT NULL
GROUP BY ZVALUESTRING
ORDER BY total_seconds DESC
LIMIT 1;
