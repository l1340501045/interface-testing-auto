-- 在迁移后的专用测试数据库中使用 psql -v ON_ERROR_STOP=1 执行。
-- 表与列的中文业务说明必须真实写入 PostgreSQL，不能只存在 ORM 或设计文件中。
DO $$
DECLARE
    missing_count integer;
BEGIN
    SELECT count(*) INTO missing_count
    FROM (
        SELECT c.oid, 0 AS column_number
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relname <> 'alembic_version'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
          AND (nullif(btrim(obj_description(c.oid, 'pg_class')), '') IS NULL
               OR obj_description(c.oid, 'pg_class') !~ '[一-龥]')
        UNION ALL
        SELECT c.oid, a.attnum
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg_toast%'
          AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relname <> 'alembic_version'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
          AND (nullif(btrim(col_description(c.oid, a.attnum)), '') IS NULL
               OR col_description(c.oid, a.attnum) !~ '[一-龥]')
    ) missing;
    IF missing_count > 0 THEN
        RAISE EXCEPTION '存在 % 个缺少中文说明的业务表或字段', missing_count;
    END IF;
END $$;
