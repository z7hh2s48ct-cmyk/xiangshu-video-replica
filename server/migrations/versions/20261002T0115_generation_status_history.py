"""从部署本迁移起记录真实状态转换；不回填无法还原的历史。"""

from alembic import op

revision = "20261002T0115_generation_status_history"
down_revision = "20261002T0050_collection_stats_version"
branch_labels = None
depends_on = None

TABLES = {
    "generation_tasks": "VIDEO",
    "first_frame_tasks": "FIRST_FRAME_IMAGE",
    "character_sheet_tasks": "CHARACTER_SHEET_IMAGE",
    "character_generation_tasks": "CHARACTER_VIEW_IMAGE",
    "source_frame_tasks": "SOURCE_FRAME",
    "analysis_tasks": "ANALYSIS",
    "oral_tasks": "ORAL_VIDEO",
    "oral_avatars": "ORAL_AVATAR",
    "oral_voices": "ORAL_VOICE",
}


def upgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE generation_record_history_coverage (
        id boolean PRIMARY KEY DEFAULT TRUE CHECK(id),
        started_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    op.execute("INSERT INTO generation_record_history_coverage(id) VALUES(TRUE)")
    op.execute("""CREATE TABLE generation_record_status_events (
        id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
        record_type text NOT NULL, record_id text NOT NULL,
        old_status text, new_status text NOT NULL,
        occurred_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
    op.execute("""CREATE INDEX ix_generation_record_status_events_record
        ON generation_record_status_events(record_type,record_id,occurred_at,id)""")
    op.execute("""CREATE FUNCTION capture_generation_record_status() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE before_state text; after_state text;
        BEGIN
          after_state := NEW.status;
          IF TG_OP='UPDATE' THEN before_state := OLD.status; END IF;
          IF TG_ARGV[0] IN ('ORAL_AVATAR','ORAL_VOICE') THEN
            IF NEW.submission_state='SUBMISSION_UNKNOWN' THEN
              after_state := 'SUBMISSION_UNCERTAIN';
            ELSIF NEW.status='READY' THEN after_state := 'SUCCEEDED'; END IF;
            IF TG_OP='UPDATE' THEN
              IF OLD.submission_state='SUBMISSION_UNKNOWN' THEN
                before_state := 'SUBMISSION_UNCERTAIN';
              ELSIF OLD.status='READY' THEN before_state := 'SUCCEEDED'; END IF;
            END IF;
          END IF;
          IF before_state IS DISTINCT FROM after_state THEN
            INSERT INTO generation_record_status_events
              (record_type,record_id,old_status,new_status)
              VALUES(TG_ARGV[0],NEW.id,before_state,after_state);
          END IF;
          RETURN NEW;
        END $$""")
    for table, kind in TABLES.items():
        op.execute(f"""CREATE TRIGGER capture_admin_status_history
            AFTER INSERT OR UPDATE ON {table} FOR EACH ROW
            EXECUTE FUNCTION capture_generation_record_status('{kind}')""")


def downgrade() -> None:
    # These additions belong to the PG runtime, not offline legacy source snapshots.
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f"DROP TRIGGER capture_admin_status_history ON {table}")
    op.execute("DROP FUNCTION capture_generation_record_status()")
    op.execute("DROP TABLE generation_record_status_events")
    op.execute("DROP TABLE generation_record_history_coverage")
