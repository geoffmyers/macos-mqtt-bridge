-- src/macos_bridge/queries/family_top_apps_today.sql
-- Top apps by total seconds across all family devices since :since_mat.
SELECT
    ti.ZBUNDLEIDENTIFIER AS bundle_id,
    SUM(ti.ZTOTALTIMEINSECONDS) AS total_seconds
FROM ZUSAGETIMEDITEM ti
    JOIN ZUSAGECATEGORY c ON ti.ZCATEGORY = c.Z_PK
    JOIN ZUSAGEBLOCK b    ON c.ZBLOCK     = b.Z_PK
WHERE b.ZSTARTDATE >= :since_mat
  AND ti.ZBUNDLEIDENTIFIER IS NOT NULL
GROUP BY ti.ZBUNDLEIDENTIFIER
ORDER BY total_seconds DESC
LIMIT 10;
