-- src/screen_time_bridge/queries/phase_a_pickups.sql
-- Returns one row: pickup_count (INT). One pickup = display backlight turned on today.
SELECT COUNT(*) AS pickup_count
FROM ZOBJECT
WHERE ZSTREAMNAME = '/display/isBacklit'
  AND ZVALUEINTEGER = 1
  AND ZSTARTDATE >= :since_mat;
