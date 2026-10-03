-- Run once as postgres in the Supabase SQL Editor on a NEW project.
-- Passwords are intentionally absent: set them separately as explained in the guide.
-- Fails if these roles/schema already exist; inspect existing ownership before reusing.
BEGIN;
CREATE ROLE ai_sales_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE ai_sales_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT ai_sales_migrator TO postgres;
CREATE SCHEMA app AUTHORIZATION ai_sales_migrator;
REVOKE ALL ON SCHEMA app FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA app TO ai_sales_runtime;
GRANT CONNECT ON DATABASE postgres TO ai_sales_migrator, ai_sales_runtime;
-- These are new roles without memberships; PUBLIC can otherwise confer DDL access.
-- Project-wide hardening: review before applying to an existing shared project.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON DATABASE postgres FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_sales_migrator IN SCHEMA app
    REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE ai_sales_migrator IN SCHEMA app
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated, service_role;
COMMIT;
