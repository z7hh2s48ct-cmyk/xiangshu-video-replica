"""Itemized tariffs, operation accounting and new recharge funding lots.

No historical balance conversion, price backfill or operational cutover.
"""

from alembic import op

revision = "20260913T1100_itemized_billing"
down_revision = "20260913T0630_account_credit_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retained offline archive tools may traverse the chain; no billing runtime on SQLite.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE oral_tasks ALTER COLUMN duration_sec TYPE numeric(18,6);
        CREATE TABLE billing_tariffs (
          service text PRIMARY KEY,
          enabled boolean NOT NULL DEFAULT false,
          unit_credits numeric(18,6) CHECK(unit_credits BETWEEN 0 AND 1000000),
          unit_cost_fen numeric(20,6) CHECK(unit_cost_fen BETWEEN 0 AND 100000000),
          unit_rounding text NOT NULL DEFAULT 'ceil' CHECK(unit_rounding IN ('ceil','exact')),
          version integer NOT NULL DEFAULT 1 CHECK(version > 0),
          updated_by_user_id text REFERENCES users(id),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CHECK(NOT enabled OR unit_credits IS NOT NULL),
          CHECK(service NOT IN ('cos','zpay') OR (NOT enabled AND COALESCE(unit_cost_fen,0)=0)),
          CHECK(service NOT IN ('quality_inspection','analysis_repair') OR NOT enabled)
        );
        CREATE TABLE billing_operations (
          id text PRIMARY KEY,
          user_id text REFERENCES users(id),
          service text NOT NULL,
          module text NOT NULL,
          source_id text NOT NULL,
          billing_round integer NOT NULL DEFAULT 1 CHECK(billing_round > 0),
          submission_id text,
          request_fingerprint text NOT NULL DEFAULT '',
          api_key_id text REFERENCES customer_api_keys(id),
          auth_source text,
          pricing_snapshot_json text NOT NULL,
          unit text NOT NULL CHECK(unit IN ('second','image','call')),
          budget_units numeric(18,6) NOT NULL CHECK(budget_units >= 0),
          actual_units numeric(18,6) CHECK(actual_units >= 0),
          reserved_credits integer NOT NULL DEFAULT 0 CHECK(reserved_credits >= 0),
          charged_credits integer NOT NULL DEFAULT 0 CHECK(charged_credits >= 0),
          revenue_fen numeric(24,8),
          nominal_revenue_fen numeric(24,8),
          funding_json text NOT NULL DEFAULT '[]',
          state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','SUCCEEDED',
            'FAILED','CANCELLED')),
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          UNIQUE(user_id, service, source_id, billing_round),
          CHECK(user_id IS NOT NULL OR reserved_credits=0),
          CHECK(charged_credits <= reserved_credits),
          CHECK((state='PENDING') = (completed_at IS NULL))
        );
        CREATE UNIQUE INDEX uq_billing_platform_operation ON billing_operations(service,
          source_id,billing_round) WHERE user_id IS NULL;
        CREATE INDEX idx_billing_operations_reporting ON billing_operations(completed_at,
          service, user_id);
        CREATE INDEX idx_billing_operations_source ON billing_operations(source_id, billing_round);
        CREATE TABLE billing_attempts (
          id text PRIMARY KEY,
          operation_id text NOT NULL REFERENCES billing_operations(id),
          attempt_key text NOT NULL,
          service text NOT NULL,
          provider text NOT NULL,
          unit text NOT NULL CHECK(unit IN ('second','image','call')),
          unit_cost_fen numeric(20,6) CHECK(unit_cost_fen >= 0),
          usage numeric(18,6) CHECK(usage >= 0),
          cost_fen numeric(24,8) CHECK(cost_fen >= 0),
          state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','ACTUAL','UNKNOWN')),
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          UNIQUE(operation_id, service, attempt_key),
          CHECK((state='ACTUAL') = (cost_fen IS NOT NULL))
        );
        CREATE TABLE billing_credit_lots (
          id text PRIMARY KEY REFERENCES wallet_transactions(id),
          user_id text NOT NULL REFERENCES users(id),
          credits integer NOT NULL CHECK(credits > 0),
          remaining_credits integer NOT NULL CHECK(remaining_credits >= 0),
          amount_fen numeric(24,8) CHECK(amount_fen >= 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK(remaining_credits <= credits)
        );
        CREATE TABLE billing_evidence (
          id text PRIMARY KEY,
          operation_id text NOT NULL REFERENCES billing_operations(id),
          attempt_id text UNIQUE REFERENCES billing_attempts(id),
          units numeric(18,6) CHECK(units > 0),
          cost_fen numeric(24,8) CHECK(cost_fen >= 0),
          reference text NOT NULL CHECK(length(reference) BETWEEN 1 AND 500),
          reason text NOT NULL,
          actor_user_id text NOT NULL REFERENCES users(id),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK((attempt_id IS NULL AND units IS NOT NULL AND cost_fen IS NULL)
             OR (attempt_id IS NOT NULL AND units IS NULL AND cost_fen IS NOT NULL))
        );
        CREATE INDEX idx_billing_credit_lots_wallet ON billing_credit_lots(user_id,
          created_at, id);
        CREATE FUNCTION billing_capture_charge() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.type = 'CHARGE' THEN
            INSERT INTO billing_credit_lots(id,user_id,credits,remaining_credits,amount_fen)
            SELECT NEW.id,NEW.user_id,NEW.available_delta,NEW.available_delta,
              CASE WHEN o.provider IN ('zpay','wechat_native') THEN o.amount_fen
                   WHEN o.amount_fen=0 THEN 0 ELSE NULL END
            FROM recharge_orders o WHERE o.id=NEW.recharge_order_id;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_billing_capture_charge AFTER INSERT ON wallet_transactions
          FOR EACH ROW EXECUTE FUNCTION billing_capture_charge();
        ALTER TABLE wallet_transactions ADD COLUMN billing_operation_id text REFERENCES
          billing_operations(id);
        ALTER TABLE wallet_transactions DROP CONSTRAINT ck_wallet_transactions_shape;
        ALTER TABLE wallet_transactions ADD CONSTRAINT ck_wallet_transactions_shape CHECK (
          (type='CHARGE' AND available_delta>0 AND reserved_delta=0 AND recharge_order_id IS
            NOT NULL
           AND task_id IS NULL AND oral_task_id IS NULL AND billing_operation_id IS NULL AND
             billing_round IS NULL)
          OR (type='CONVERSION' AND available_delta<>0 AND reserved_delta=0 AND
            recharge_order_id IS NULL
           AND task_id IS NULL AND oral_task_id IS NULL AND billing_operation_id IS NULL AND
             billing_round IS NULL)
          OR (recharge_order_id IS NULL AND billing_round IS NOT NULL
           AND ((billing_operation_id IS NULL AND num_nonnulls(task_id,oral_task_id)=1)
           OR (billing_operation_id IS NOT NULL AND num_nonnulls(task_id,oral_task_id)<=1)) AND (
            (type='RESERVE' AND available_delta=-reserved_delta AND reserved_delta>=1)
            OR (type='SETTLE' AND available_delta=0 AND reserved_delta<=-1)
            OR (type='RELEASE' AND available_delta=-reserved_delta AND available_delta>=1 AND
              reserved_delta<=-1)))
        );
        DROP INDEX uq_wallet_transactions_terminal_round;
        CREATE UNIQUE INDEX uq_wallet_transactions_terminal_round
          ON wallet_transactions(task_id,billing_round)
          WHERE task_id IS NOT NULL AND type IN ('SETTLE','RELEASE') AND billing_operation_id
            IS NULL;
        DROP INDEX uq_wallet_transactions_oral_terminal_round;
        CREATE UNIQUE INDEX uq_wallet_transactions_oral_terminal_round
          ON wallet_transactions(oral_task_id,billing_round)
          WHERE oral_task_id IS NOT NULL AND type IN ('SETTLE','RELEASE') AND
            billing_operation_id IS NULL;
        CREATE UNIQUE INDEX uq_billing_operation_ledger_type ON
          wallet_transactions(billing_operation_id,type)
          WHERE billing_operation_id IS NOT NULL;
        CREATE FUNCTION billing_refuse_fact_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='TRUNCATE' OR TG_TABLE_NAME='billing_evidence' THEN
            RAISE EXCEPTION 'billing evidence is append only';
          END IF;
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'billing facts cannot be deleted'; END IF;
          IF TG_TABLE_NAME='billing_operations' THEN
            IF (to_jsonb(OLD)-ARRAY['state','actual_units','charged_credits','revenue_fen',
              'nominal_revenue_fen','completed_at'])
                IS DISTINCT FROM
               (to_jsonb(NEW)-ARRAY['state','actual_units','charged_credits','revenue_fen',
                 'nominal_revenue_fen','completed_at'])
              OR (OLD.state<>'PENDING' AND NEW IS DISTINCT FROM OLD)
            THEN RAISE EXCEPTION
              'accepted billing snapshots and terminal facts are immutable'; END IF;
          ELSIF TG_TABLE_NAME='billing_attempts' THEN
            IF (to_jsonb(OLD)-ARRAY['state','usage','cost_fen','completed_at']) IS DISTINCT FROM
               (to_jsonb(NEW)-ARRAY['state','usage','cost_fen','completed_at'])
              OR (OLD.state<>'PENDING' AND NEW IS DISTINCT FROM OLD)
            THEN RAISE EXCEPTION 'provider rate snapshots and completed costs are immutable';
              END IF;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_billing_operations_immutable BEFORE UPDATE OR DELETE ON
          billing_operations
          FOR EACH ROW EXECUTE FUNCTION billing_refuse_fact_rewrite();
        CREATE TRIGGER trg_billing_attempts_immutable BEFORE UPDATE OR DELETE ON billing_attempts
          FOR EACH ROW EXECUTE FUNCTION billing_refuse_fact_rewrite();
        CREATE TRIGGER trg_billing_evidence_immutable BEFORE UPDATE OR DELETE ON billing_evidence
          FOR EACH ROW EXECUTE FUNCTION billing_refuse_fact_rewrite();
        CREATE TRIGGER trg_billing_operations_no_truncate BEFORE TRUNCATE ON billing_operations
          FOR EACH STATEMENT EXECUTE FUNCTION billing_refuse_fact_rewrite();
        CREATE TRIGGER trg_billing_attempts_no_truncate BEFORE TRUNCATE ON billing_attempts
          FOR EACH STATEMENT EXECUTE FUNCTION billing_refuse_fact_rewrite();
        CREATE TRIGGER trg_billing_evidence_no_truncate BEFORE TRUNCATE ON billing_evidence
          FOR EACH STATEMENT EXECUTE FUNCTION billing_refuse_fact_rewrite();
        CREATE VIEW billing_effective_attempts AS
          SELECT a.*,COALESCE(e.cost_fen,a.cost_fen) AS effective_cost_fen,
            e.reference AS evidence_reference FROM billing_attempts a
          LEFT JOIN billing_evidence e ON e.attempt_id=a.id;
        ALTER TABLE operation_cost_records ALTER COLUMN usage_amount TYPE numeric(18,6);
        ALTER TABLE operation_cost_records ALTER COLUMN cost_fen TYPE numeric(24,8);
        ALTER TABLE operation_cost_records ALTER COLUMN unit_price_fen DROP NOT NULL;
        ALTER TABLE operation_cost_records ALTER COLUMN unit_price_fen TYPE numeric(20,6);
        ALTER TABLE operation_cost_records ADD COLUMN billing_attempt_id text REFERENCES
          billing_attempts(id);
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # Financial facts cannot be discarded by a schema rollback.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM billing_operations) OR EXISTS(SELECT 1 FROM billing_credit_lots)
            OR EXISTS(SELECT 1 FROM operation_cost_records WHERE unit_price_fen IS NULL OR
              unit_price_fen<>trunc(unit_price_fen))
            OR EXISTS(SELECT 1 FROM oral_tasks WHERE duration_sec<>trunc(duration_sec))
            OR EXISTS(SELECT 1 FROM operation_cost_records WHERE
              usage_amount<>round(usage_amount,3) OR cost_fen<>round(cost_fen,3))
          THEN RAISE EXCEPTION
            'itemized billing contains financial facts; forward repair required'; END IF;
        END $$;
        DROP TRIGGER trg_billing_capture_charge ON wallet_transactions;
        DROP FUNCTION billing_capture_charge();
        ALTER TABLE operation_cost_records DROP COLUMN billing_attempt_id;
        ALTER TABLE operation_cost_records ALTER COLUMN unit_price_fen TYPE integer;
        ALTER TABLE operation_cost_records ALTER COLUMN usage_amount TYPE numeric(14,3);
        ALTER TABLE operation_cost_records ALTER COLUMN cost_fen TYPE numeric(16,3);
        ALTER TABLE operation_cost_records ALTER COLUMN unit_price_fen SET NOT NULL;
        ALTER TABLE oral_tasks ALTER COLUMN duration_sec TYPE integer;
        ALTER TABLE wallet_transactions DROP CONSTRAINT ck_wallet_transactions_shape;
        ALTER TABLE wallet_transactions DROP COLUMN billing_operation_id;
        CREATE UNIQUE INDEX uq_wallet_transactions_terminal_round ON
          wallet_transactions(task_id,billing_round)
          WHERE task_id IS NOT NULL AND type IN ('SETTLE','RELEASE');
        CREATE UNIQUE INDEX uq_wallet_transactions_oral_terminal_round ON
          wallet_transactions(oral_task_id,billing_round)
          WHERE oral_task_id IS NOT NULL AND type IN ('SETTLE','RELEASE');
        DROP VIEW billing_effective_attempts;
        DROP TABLE billing_evidence, billing_attempts, billing_operations,
          billing_credit_lots, billing_tariffs;
        DROP FUNCTION billing_refuse_fact_rewrite();
    """)
    # Restore the exact prior ledger shape; existing cost precision is retained losslessly.
    import importlib

    old = importlib.import_module("migrations.versions.20260913T0630_account_credit_operations")
    op.create_check_constraint(
        "ck_wallet_transactions_shape",
        "wallet_transactions",
        old._OLD_SHAPE + " OR (type = 'CONVERSION' AND available_delta <> 0 AND reserved_delta = 0 "
        "AND recharge_order_id IS NULL AND task_id IS NULL AND oral_task_id IS NULL AND "
        "billing_round IS NULL)",
    )
