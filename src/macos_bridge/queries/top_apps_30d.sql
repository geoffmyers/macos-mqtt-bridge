-- src/screen_time_bridge/queries/phase_b_top_apps_30d.sql
-- Top-100 apps by usage seconds since :since_mat (caller passes 30d ago).
-- Used by `screen-time-ha-bridge bootstrap-allowlist`. Caller filters by
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
