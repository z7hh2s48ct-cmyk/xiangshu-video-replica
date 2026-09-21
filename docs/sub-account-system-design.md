# Sub-Account Management System Design (CW-062)

## Overview

This document describes the sub-account management system implemented in CW-062, enabling master accounts to create and manage subordinate accounts for distributed video replication operations while maintaining centralized billing and resource ownership.

## Architecture

### Data Model

```mermaid
erDiagram
    users ||--o{ customer_devices : "owns"
    users ||--o{ publish_accounts : "owns"
    users ||--o{ wallet_transactions : "generates"
    users {
        TEXT id PK
        TEXT username UK
        TEXT display_name
        TEXT parent_user_id FK->users.id
        TEXT account_type ENUM('MASTER','SUB')
        TEXT role ENUM('customer','admin')
        BOOLEAN is_active
        TIMESTAMP created_at
        TIMESTAMP updated_at
    }
    customer_devices {
        TEXT id PK
        TEXT user_id FK->users.id
        TEXT parent_user_id FK->users.id ON DELETE SET NULL
        INTEGER slot_no
        TIMESTAMP bound_at
    }
    publish_accounts {
        TEXT id PK
        TEXT user_id FK->users.id
        TEXT platform
        TEXT display_name
        TIMESTAMP created_at
    }
```

### Cascade Relationships

When a MASTER account is deleted:

1. **Sub-accounts**: CASCADE DELETE (all children removed)
2. **Devices**: `parent_user_id` → NULL (preserves history, breaks binding)
3. **Publish Accounts**: No automatic change (`user_id` still points to deleted master)
4. **Wallets/Money**: Unchanged (financial audit trail preserved)

**Design Rationale**: 
- Devices stay in DB for compliance but become "orphaned" (`parent_user_id=NULL`)
- Publish accounts retained for record-keeping
- Only immediate hierarchy (sub-accounts) fully cascades

---

## API Endpoints

### Base URL

```
/api/admin/sub-accounts
```

All endpoints require admin role authentication.

### 1. Create Sub-Account

**POST** `/api/admin/sub-accounts`

**Request Body**:
```json
{
  "username": "employee_001",
  "display_name": "张三 (员工)",
  "parent_user_id": "master_uuid_here",
  "reason": "用于视频批量生产",
  "request_id": "uuid-v4"
}
```

**Validation**:
- `username`: Unique across all users (checked via `SELECT 1 FROM users WHERE username = %s`)
- `parent_user_id`: Must exist AND have `account_type='MASTER'` AND `role='customer'`
- `reason`: Non-blank string (for audit log)

**Success Response (200)**:
```json
{
  "id": "sub_a1b2c3d4e5f6",
  "username": "employee_001",
  "display_name": "张三 (员工)",
  "account_type": "SUB",
  "parent_user_id": "master_uuid_here",
  "is_active": true,
  "created_at": "2026-09-19T12:34:56.789Z"
}
```

**Errors**:
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `PARENT_NOT_FOUND` | 400 | Parent ID doesn't exist |
| `PARENT_NOT_MASTER` | 400 | Parent exists but isn't MASTER type |
| `PARENT_NOT_CUSTOMER_ROLE` | 400 | Parent exists but role≠'customer' |
| `USERNAME_EXISTS` | 400 | Username already taken |
| `ADMIN_REQUIRED` | 403 | Caller not an admin |

---

### 2. List Sub-Accounts

**GET** `/api/admin/sub-accounts?parent_user_id={id}`

**Query Parameters**:
| Name | Required | Type | Description |
|------|----------|------|-------------|
| `parent_user_id` | Yes | UUID | Master account ID to list children under |

**Response (200)**:
```json
{
  "sub_accounts": [
    {
      "id": "sub_xyz123",
      "username": "employee_001",
      "display_name": "张三 (员工)",
      "parent_user_id": "master_uuid_here",
      "is_active": true,
      "created_at": "2026-09-19T12:34:56.789Z",
      "updated_at": null
    }
  ],
  "total_count": 1
}
```

**Ordering**: DESC by `created_at`, then ASC by `id` (deterministic tie-breaker)

---

### 3. Update Sub-Account

**PATCH** `/api/admin/sub-accounts/{sub_account_id}`

**Request Body** (partial updates allowed):
```json
{
  "display_name": "新名称",
  "is_active": false,
  "reason": "调整信息",
  "request_id": "uuid-v4"
}
```

**Validation**:
- `sub_account_id`: Must exist AND have `account_type='SUB'`
- At least one of `display_name` or `is_active` must be non-null

**Response (200)**: Full updated user object

**Errors**:
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `SUB_ACCOUNT_NOT_FOUND` | 404 | ID doesn't exist or isn't SUB type |
| `ADMIN_REQUIRED` | 403 | Caller not an admin |

---

### 4. Delete Sub-Account

**DELETE** `/api/admin/sub-accounts/{sub_account_id}`

**Response (200)**:
```json
{
  "deleted": true,
  "sub_account_id": "sub_xyz123"
}
```

**Cascade Effects**:
- All devices with `parent_user_id = sub_account_id` → `parent_user_id` becomes NULL
- No changes to `user_id` references in `publish_accounts`
- Audit log entry created (action: `'sub_account.delete'`)

**Errors**:
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `SUB_ACCOUNT_NOT_FOUND` | 404 | ID doesn't exist |
| `ADMIN_REQUIRED` | 403 | Caller not an admin |

---

## Authorization & Access Control

### T2.8: Sub-Account Access Validator

**Function**: `ensure_sub_account_access(current_user)`

Ensures caller has:
- `account_type == 'SUB'`
- `is_active == true`
- `parent_user_id != NULL`

**Usage Pattern**:
```python
@router.post("/sub-accounts/{id}/actions")
async def do_action(
    current_user: Annotated[CurrentUser, Security(ensure_sub_account_access)]
):
    # Now guaranteed: caller is active SUB
    parent_id = current_user.parent_user_id
    ...
```

---

### T2.9: Cross-Parent Access Prevention

**Function**: `prevent_cross_parent_access(current_user, target_parent_user_id)`

Enforces isolation between different master organizations:

```python
def prevent_cross_parent_access(
    current_user: CurrentUser,
    *,
    target_parent_user_id: str | None = None,
    resource_owner_id: str | None = None,
) -> None:
    """Prevent one sub-account from accessing another's resources."""
    
    # Caller MUST be SUB
    caller_is_sub = getattr(current_user, "account_type", None) == "SUB"
    if not caller_is_sub:
        return  # Masters have full access to their own resources
    
    caller_parent = current_user.parent_user_id
    
    # Check against target
    if caller_parent != target_parent:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CROSS_PARENT_ACCESS_FORBIDDEN",
                "message": "子账号只能访问同一母账号下的资源。当前父账号：{caller_parent}, 目标资源父账号：{target_parent}",
            },
        )
```

**Example Usage**:
```python
@router.get("/devices")
async def list_my_devices(
    current_user: CurrentUser,
    conn: Database,
):
    # Query devices where user_id=sub-account OR parent_user_id=sub-account
    devices = conn.execute(
        """
        SELECT * FROM customer_devices
        WHERE user_id = %s OR parent_user_id = %s
        """,
        (current_user.user_id, current_user.parent_user_id),
    ).fetchall()
    
    # Alternative: query resources and validate before returning
    for device in devices:
        prevent_cross_parent_access(
            current_user,
            target_parent_user_id=device.get("parent_user_id"),
        )
    
    return {"devices": [...]}
```

---

## Billing & Actor Tracking (T2.10)

### Wallet Transaction Schema

Modified in Phase 1 migration `20260919T1300_wallet_actor.py`:

```sql
ALTER TABLE wallet_transactions
ADD COLUMN actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX idx_wallet_transactions_actor ON wallet_transactions(actor_user_id);
```

### Semantics

| `actor_user_id` Value | Meaning |
|-----------------------|---------|
| `NULL` | Operator unknown or deleted sub-account (historical rows after backfill) |
| `user_id` (same as `wallet.user_id`) | Master account operating on its own behalf |
| `distinct ID` (FK→users, `account_type='SUB'`) | Sub-account consuming under mother's wallet |

### Integration Pattern

```python
def charge_collection(conn, *, user_id: str, units: int, caller_info: dict):
    """Charge wallet for viral collection with explicit actor tracking."""
    
    # Determine actor based on caller type
    if caller_info["type"] == "SUB":
        actor_user_id = caller_info["sub_account_id"]
    else:
        actor_user_id = user_id  # Master operates self
    
    conn.execute(
        """
        INSERT INTO wallet_transactions 
          (id, user_id, actor_user_id, provider, amount_fen, available_delta,
           task_id, status, operation_type)
        VALUES (%s, %s, %s, 'viral_collection', %s, %s, NULL, 'PAID', 'collection_charge')
        """,
        (tx_id, user_id, actor_user_id, credits, delta_credits)
    )
```

### Backfill Rule (F-6)

Migration includes:
```sql
UPDATE wallet_transactions
SET actor_user_id = user_id
WHERE actor_user_id IS NULL
  AND EXISTS (
    SELECT 1 FROM users u WHERE u.id = wallet_transactions.user_id
    AND u.account_type = 'MASTER'
  );
```

**Result after backfill**: `actor_user_id = NULL` only for historical transactions where the actual operator was later deleted (retained for audit integrity).

---

## Database Migration Guide

### Prerequisites

- PostgreSQL 16+ (production customer lane)
- Alembic 1.13+ installed
- Read/write access to production DB (`DATABASE_URL` configured)

### Steps

1. **Backup**: Always backup before migrations
   ```bash
   pg_dump -h $HOST -U $USER -d $DB_NAME > backup_before_cw062.sql
   ```

2. **Run migration**:
   ```bash
   cd server
   alembic upgrade head
   ```

3. **Verify schema**:
   ```bash
   python -m server.tests.test_cw056_supported_head_matrix --check-schema
   ```

Expected output:
```
schema guard OK ✓
HEAD_REVISION = 20260919T1500_device_parent_cascade
```

### Rollback Plan

If issues arise:
```bash
# Downgrade step-by-step
alembic downgrade 20260919T1300_wallet_actor  # Reverts T2.10 actor column
alembic downgrade 20260919T1200_publish_account_avatar
```

**Caution**: Downgrading `20260919T1500_device_parent_cascade` requires empty `customer_devices.parent_user_id` values first.

---

## Monitoring & Troubleshooting

### Key Metrics

| Metric | Source | Alert Threshold |
|--------|--------|-----------------|
| `active_sub_accounts_count` | `SELECT count(*) FROM users WHERE account_type='SUB' AND is_active=true` | >1000 per master → review ratio |
| `orphaned_devices_count` | `SELECT count(*) FROM customer_devices WHERE parent_user_id IS NULL` | Sudden increase → possible cascade delete |
| `billing_actor_null_rate` | `% of wallet_transactions where actor_user_id IS NULL` | Rising → indicates old deleted operators |

### Common Issues

#### Issue 1: Sub-account creation fails with "PARENT_NOT_MASTER"

**Cause**: Attempting to create hierarchy like SUB→SUB (not allowed)

**Fix**: Use existing MASTER as parent, or escalate privilege transfer first

#### Issue 2: Cross-parent access error on valid operation

**Cause**: Route missing `prevent_cross_parent_access()` middleware call

**Fix**: Add validation guard before returning resource data

#### Issue 3: Backfill leaves NULL actor_user_id unexpectedly

**Cause**: Historical row truly had deleted sub-account as operator

**Action**: Acceptable – preserves audit trail accurately

---

## Testing Strategy

### Unit Tests

- `test_sub_account_schema.py`: PG-only schema constraints (6 tests)
- `test_sub_account_crud.py`: API endpoint integration (9 tests)

### E2E Tests (Pending)

- Playwright flow: login as master → create/edit/delete sub-account
- Verification: check dashboard shows new child immediately

### Performance Tests

- Load test: 100 concurrent sub-account creations under single master
- Expected p95 latency < 200ms per request

---

## Future Enhancements (Post-MVP)

### Phase 4 Considerations

1. **Admin approval workflow**: Require super-admin consent before activation
2. **Device sharing pool**: Allow multiple sub-accounts to share same physical device
3. **Billing quotas**: Set monthly credit limits per sub-account
4. **Hierarchy depth limits**: Enforce max nesting level (currently flat: MASTER→SUB only)

### Technical Debt

- [ ] Migrate hardcoded error codes to centralized catalog
- [ ] Add OpenAPI response schemas for all endpoints
- [ ] Implement rate limiting for sub-account mutation APIs
- [ ] Add soft-delete flag instead of hard CASCADE DELETE

---

## References

- [CW-062 Task List](../docs/cw-062-task-list.md)
- [Phase 1 Implementation Notes](../CHANGELOG.md#phase-1-database-foundation)
- [Phase 2 Implementation Notes](../CHANGELOG.md#phase-2-backend-apis)
- [AGENTS.md Docker/Worktree Rules](../../AGENTS.md)