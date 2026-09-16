-- src/screen_time_bridge/queries/phase_d_per_pair_today.sql
-- Per-(user, device) totals since :since_mat (typically start-of-today MAT).
-- Returns one row per ZUSAGE record. Pairs with no blocks get 0/0.
SELECT
    z.Z_PK   AS usage_pk,
    u.ZDSID  AS user_dsid,
    d.Z_PK   AS device_pk,
    COALESCE(SUM(b.ZSCREENTIMEINSECONDS), 0)                       AS total_seconds,
    COALESCE(SUM(b.ZNUMBEROFPICKUPSWITHOUTAPPLICATIONUSAGE), 0)    AS total_pickups,
    MAX(b.ZSTARTDATE)                                              AS latest_block_mat
FROM ZUSAGE z
    JOIN ZCOREUSER u   ON z.ZUSER   = u.Z_PK
    JOIN ZCOREDEVICE d ON z.ZDEVICE = d.Z_PK
    LEFT JOIN ZUSAGEBLOCK b
        ON b.ZUSAGE = z.Z_PK
       AND b.ZSTARTDATE >= :since_mat
GROUP BY z.Z_PK
ORDER BY u.ZGIVENNAME, d.ZNAME;
