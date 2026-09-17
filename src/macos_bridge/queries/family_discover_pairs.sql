-- src/macos_bridge/queries/family_discover_pairs.sql
-- Discover every (user, device) pair that has at least one ZUSAGE row.
-- Used at agent startup to auto-generate one HA sensor per pair.
SELECT
    z.Z_PK         AS usage_pk,
    u.Z_PK         AS user_pk,
    u.ZDSID        AS user_dsid,
    u.ZGIVENNAME   AS user_name,
    u.ZISFAMILYORGANIZER AS is_organizer,
    d.Z_PK         AS device_pk,
    d.ZNAME        AS device_name,
    d.ZPLATFORM    AS device_platform,
    d.ZIDENTIFIER  AS device_identifier
FROM ZUSAGE z
    JOIN ZCOREUSER u   ON z.ZUSER   = u.Z_PK
    JOIN ZCOREDEVICE d ON z.ZDEVICE = d.Z_PK
ORDER BY u.ZGIVENNAME, d.ZNAME;
