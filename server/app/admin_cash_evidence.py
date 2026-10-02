"""Reporting evidence only: a compensation credit is not a cash receipt."""

# All consumers alias recharge_orders as ro. Do not infer cash from a computed amount.
OFFLINE_CASH_EVIDENCE = """(
 COALESCE(ro.payment_method='offline',false) OR EXISTS (
   SELECT 1 FROM admin_adjustments cash_aa
   WHERE cash_aa.recharge_order_id=ro.id
     AND cash_aa.source_document_type='OFFLINE_PAYMENT'
     AND length(trim(cash_aa.source_document_ref))>0
 ))"""
NONCASH_ADJUSTMENT = """EXISTS (
 SELECT 1 FROM admin_adjustments noncash_aa
 WHERE noncash_aa.recharge_order_id=ro.id
   AND noncash_aa.source_document_type IN (
     'CS_TICKET','COMPENSATION_APPROVAL','FREE_GRANT','CREDIT_COMPENSATION')
)"""
CASH_ORDER_ELIGIBLE = (
    f"(ro.provider<>'admin_adjustment' OR {OFFLINE_CASH_EVIDENCE}) AND NOT ({NONCASH_ADJUSTMENT})"
)
