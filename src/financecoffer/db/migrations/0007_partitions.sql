-- 0007 partitions: monthly RANGE partition maintenance for ods.stock_raw_data.
-- `db migrate` pre-creates the current month plus 3 ahead; ETL may call
-- ods.ensure_partition() before an insert to cover a late event. Boundaries are
-- pinned to UTC so a server timezone change never shifts existing ranges.

CREATE OR REPLACE FUNCTION ods.partition_name(target_month date)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT 'stock_raw_data_' || to_char(date_trunc('month', target_month), 'YYYYMM');
$$;

CREATE OR REPLACE FUNCTION ods.ensure_partition(target_month date)
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    month_start date := date_trunc('month', target_month)::date;
    month_end   date := (date_trunc('month', target_month) + interval '1 month')::date;
    part_name   text := ods.partition_name(target_month);
    lower_bound text := to_char(month_start, 'YYYY-MM-DD') || ' 00:00:00+00';
    upper_bound text := to_char(month_end, 'YYYY-MM-DD') || ' 00:00:00+00';
BEGIN
    IF to_regclass(format('ods.%I', part_name)) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE ods.%I PARTITION OF ods.stock_raw_data'
            ' FOR VALUES FROM (%L) TO (%L)',
            part_name, lower_bound, upper_bound
        );
    END IF;
    RETURN part_name;
END;
$$;

CREATE OR REPLACE FUNCTION ods.ensure_partitions(ahead_months integer DEFAULT 3)
RETURNS SETOF text
LANGUAGE plpgsql
AS $$
DECLARE
    i integer;
    base_month date := date_trunc('month', (now() AT TIME ZONE 'UTC')::date)::date;
BEGIN
    IF ahead_months < 0 THEN
        RAISE EXCEPTION 'ahead_months must be >= 0' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR i IN 0..ahead_months LOOP
        RETURN NEXT ods.ensure_partition((base_month + make_interval(months => i))::date);
    END LOOP;
END;
$$;

COMMENT ON FUNCTION ods.ensure_partition(date) IS
    'Create the monthly partition covering target_month; returns the table name.';
COMMENT ON FUNCTION ods.ensure_partitions(integer) IS
    'Create partitions from the current UTC month through +ahead_months.';
