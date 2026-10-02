"""Durable unique customer references and enqueue/worker/audit correlation."""

from alembic import op

revision = "20261002T0630_task_diagnostic_refs"
down_revision = "20261002T0600_call_diagnostics"
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
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""CREATE TABLE task_diagnostic_refs (
        task_type text NOT NULL, task_id text NOT NULL,
        short_ref text NOT NULL UNIQUE CHECK(short_ref ~ '^[0-9A-F]{8}$'),
        request_id text NOT NULL, enqueue_request_id text,
        parent_task_id text, root_task_id text NOT NULL, batch_id text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(task_type, task_id))""")
    op.execute("CREATE INDEX task_diagnostic_request_idx ON task_diagnostic_refs(request_id)")
    op.execute(
        "CREATE INDEX task_diagnostic_root_idx ON task_diagnostic_refs(task_type,root_task_id)"
    )
    op.execute("""CREATE FUNCTION register_task_diagnostic_ref(kind text, task text, payload jsonb)
        RETURNS void LANGUAGE plpgsql AS $$
        DECLARE candidate text; collision integer := 0; parent_ref record;
                enqueue_ref text; parent_id text; root_id text; correlation text;
        BEGIN
          IF EXISTS(SELECT 1 FROM task_diagnostic_refs WHERE task_type=kind AND task_id=task)
            THEN RETURN; END IF;
          enqueue_ref := NULLIF(current_setting('app.request_id',true),'');
          parent_id := NULLIF(payload->>'retry_of_task_id','');
          SELECT * INTO parent_ref FROM task_diagnostic_refs
            WHERE task_type=kind AND task_id=parent_id;
          root_id := COALESCE(parent_ref.root_task_id, task);
          correlation := COALESCE(parent_ref.request_id, enqueue_ref,
                                  'task_' || kind || '_' || task);
          LOOP
            candidate := upper(substr(md5(kind || ':' || task || ':' || collision::text),1,8));
            INSERT INTO task_diagnostic_refs(task_type,task_id,short_ref,request_id,
              enqueue_request_id,parent_task_id,root_task_id,batch_id)
            VALUES(kind,task,candidate,correlation,enqueue_ref,parent_id,root_id,payload->>'batch_id')
            ON CONFLICT DO NOTHING;
            IF FOUND OR EXISTS(SELECT 1 FROM task_diagnostic_refs
                              WHERE task_type=kind AND task_id=task) THEN RETURN; END IF;
            collision := collision + 1;
            IF collision > 4096 THEN
              RAISE EXCEPTION 'diagnostic reference capacity exhausted';
            END IF;
          END LOOP;
        END $$""")
    op.execute("""CREATE FUNCTION capture_task_diagnostic_ref() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          PERFORM register_task_diagnostic_ref(TG_ARGV[0],NEW.id,to_jsonb(NEW));
          RETURN NEW;
        END $$""")
    for table, kind in TABLES.items():
        op.execute(f"""SELECT register_task_diagnostic_ref('{kind}',id,to_jsonb(task))
            FROM {table} task ORDER BY created_at,id""")
        op.execute(f"""CREATE TRIGGER capture_task_diagnostic_ref AFTER INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION capture_task_diagnostic_ref('{kind}')""")
    # Backfill known retry ancestry only from the persisted relationship, never by time/provider ID.
    op.execute("""WITH RECURSIVE ancestry AS (
        SELECT task_id,task_id AS root_id,request_id FROM task_diagnostic_refs
        WHERE task_type='VIDEO' AND parent_task_id IS NULL
        UNION ALL
        SELECT child.task_id,ancestor.root_id,ancestor.request_id
        FROM task_diagnostic_refs child JOIN ancestry ancestor
          ON child.parent_task_id=ancestor.task_id
        WHERE child.task_type='VIDEO')
        UPDATE task_diagnostic_refs target SET root_task_id=ancestry.root_id,
          request_id=ancestry.request_id FROM ancestry WHERE target.task_type='VIDEO'
          AND target.task_id=ancestry.task_id""")
    op.execute("""CREATE FUNCTION capture_audit_diagnostic_ref() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE metadata jsonb; request_ref text; task_ref text; trace_ref text;
        BEGIN
          metadata := COALESCE(NULLIF(NEW.metadata_json,'')::jsonb,'{}'::jsonb);
          request_ref := NULLIF(current_setting('app.request_id',true),'');
          task_ref := COALESCE(NULLIF(current_setting('app.task_id',true),''),
                               metadata->>'task_id',NEW.entity_id);
          SELECT request_id INTO trace_ref FROM task_diagnostic_refs WHERE task_id=task_ref LIMIT 1;
          IF request_ref IS NOT NULL THEN
            metadata := metadata || jsonb_build_object('request_id',request_ref);
          END IF;
          IF trace_ref IS NOT NULL THEN
            metadata := metadata || jsonb_build_object(
              'task_id',task_ref,'trace_request_id',trace_ref);
          END IF;
          NEW.metadata_json := metadata::text;
          RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER capture_audit_diagnostic_ref BEFORE INSERT ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION capture_audit_diagnostic_ref()""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER capture_audit_diagnostic_ref ON audit_logs")
    op.execute("DROP FUNCTION capture_audit_diagnostic_ref()")
    for table in TABLES:
        op.execute(f"DROP TRIGGER capture_task_diagnostic_ref ON {table}")
    op.execute("DROP FUNCTION capture_task_diagnostic_ref()")
    op.execute("DROP FUNCTION register_task_diagnostic_ref(text,text,jsonb)")
    op.execute("DROP TABLE task_diagnostic_refs")
