-- src/macos_bridge/queries/per_app_today.sql
-- Per-app usage seconds today, grouped by bundle_id.
-- Caller filters to the allow-listed bundle IDs in Python.
SELECT ZVALUESTRING AS bundle_id,
       SUM(ZENDDATE - ZSTARTDATE) AS total_seconds
FROM ZOBJECT
WHERE ZSTREAMNAME = '/app/usage'
  AND ZSTARTDATE >= :since_mat
  AND ZVALUESTRING IS NOT NULL
GROUP BY ZVALUESTRING;
