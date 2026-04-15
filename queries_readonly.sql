-- Connect with:
-- docker compose exec db psql -U user -d viscane_db
-- Run with:
-- docker compose exec -T db psql -U user -d viscane_db < queries_readonly.sql
-- Safe to run for inspection only.

-- Inspect
\dt
\d "user"
\d admin
\d scan
\d audit_log
\d system_config
\d notification
\d feedback
\d agronomic_log

-- Users / Farmers
SELECT * FROM "user" ORDER BY id;

SELECT id, fullname, email, phone, province, municipality, barangay, is_active, is_archived, created_at
FROM "user"
ORDER BY id;

SELECT * FROM "user"
WHERE is_active = TRUE AND is_archived = FALSE
ORDER BY id;

SELECT * FROM "user"
WHERE is_active = FALSE AND is_archived = FALSE
ORDER BY id;

SELECT * FROM "user"
WHERE is_archived = TRUE
ORDER BY id;

SELECT COUNT(*) FROM "user";

SELECT
  COUNT(*) FILTER (WHERE is_active = TRUE AND is_archived = FALSE) AS active,
  COUNT(*) FILTER (WHERE is_active = FALSE AND is_archived = FALSE) AS deactivated,
  COUNT(*) FILTER (WHERE is_archived = TRUE) AS archived
FROM "user";

SELECT * FROM "user"
WHERE fullname ILIKE '%john%' OR email ILIKE '%john%';

-- Admins
SELECT * FROM admin ORDER BY id;

SELECT id, username, email, role, is_archived
FROM admin
ORDER BY id;

-- Scans
SELECT * FROM scan ORDER BY id;

SELECT id, user_id, plot_name, grade, maturity_pct, status, created_at
FROM scan
ORDER BY created_at DESC;

SELECT * FROM scan WHERE user_id = 1 ORDER BY created_at DESC;

SELECT status, COUNT(*) FROM scan GROUP BY status ORDER BY status;

SELECT u.fullname, s.plot_name, s.grade, s.maturity_pct, s.status, s.created_at
FROM scan s
JOIN "user" u ON u.id = s.user_id
ORDER BY s.created_at DESC;

-- Audit logs
SELECT * FROM audit_log ORDER BY timestamp DESC;

SELECT * FROM audit_log
WHERE user_id = 1
ORDER BY timestamp DESC;

-- System config
SELECT * FROM system_config;

-- Notifications
SELECT * FROM notification ORDER BY created_at DESC;

-- Feedback
SELECT * FROM feedback ORDER BY created_at DESC;

SELECT f.id, u.fullname, f.message, f.created_at
FROM feedback f
LEFT JOIN "user" u ON u.id = f.user_id
ORDER BY f.created_at DESC;

-- Agronomic logs
SELECT * FROM agronomic_log ORDER BY created_at DESC;

SELECT * FROM agronomic_log
WHERE user_id = 1
ORDER BY created_at DESC;

SELECT u.fullname, a.variety, a.hectares, a.predicted_lkg_tc, a.predicted_tc_ha, a.predicted_lkg, a.created_at
FROM agronomic_log a
JOIN "user" u ON u.id = a.user_id
ORDER BY a.created_at DESC;

-- Quit
\q
