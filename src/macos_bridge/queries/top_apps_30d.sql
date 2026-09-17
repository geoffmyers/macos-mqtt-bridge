-- src/macos_bridge/queries/top_apps_30d.sql
-- Top-100 apps by usage seconds since :since_mat (caller passes 30d ago).
-- Used by `macos-mqtt-bridge bootstrap-allowlist`. Caller filters by
-- prefix/deny-pattern in Python and returns the top N.
SELECT ZVALUESTRING AS bundle_id,
       SUM(ZENDDATE - ZSTARTDATE) AS total_seconds
FROM ZOBJECT
WHERE ZSTREAMNAME = '/app/usage'
  AND ZSTARTDATE >= :since_mat
  AND ZVALUESTRING IS NOT NULL
GROUP BY ZVALUESTRING
ORDER BY total_seconds DESC
LIMIT 100;
