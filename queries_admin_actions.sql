-- Connect with:
-- docker compose exec db psql -U user -d viscane_db
-- Run with:
-- docker compose exec -T db psql -U user -d viscane_db < queries_admin_actions.sql
-- This file changes data. Run individual statements carefully.

-- Update farmer status
UPDATE "user" SET is_active = FALSE WHERE id = 1;
UPDATE "user" SET is_active = TRUE WHERE id = 1;
UPDATE "user" SET is_archived = TRUE WHERE id = 1;
UPDATE "user" SET is_archived = FALSE WHERE id = 1;

-- Insert / delete farmer
INSERT INTO "user" (fullname, email, phone, password, province, municipality, barangay, is_active, is_archived, created_at)
VALUES ('Juan Dela Cruz', 'juan@example.com', '09123456789', 'hashed_password_here', 'Negros', 'Bacolod', 'Barangay 1', TRUE, FALSE, NOW());

DELETE FROM "user" WHERE id = 1;

-- Admin updates
UPDATE admin SET role = 'superadmin' WHERE id = 1;
UPDATE admin SET is_archived = TRUE WHERE id = 1;
UPDATE admin SET is_archived = FALSE WHERE id = 1;

-- System config update
UPDATE system_config
SET system_name = 'VISCANE', maintenance_mode = FALSE
WHERE id = 1;

-- Notifications
INSERT INTO notification (title, message, created_at, created_by)
VALUES ('System Update', 'Maintenance tonight at 10 PM.', NOW(), 1);

DELETE FROM notification WHERE id = 1;

-- Useful cleanup / reset
TRUNCATE TABLE notification RESTART IDENTITY;
TRUNCATE TABLE feedback RESTART IDENTITY;

-- Quit
\q
