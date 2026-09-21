# Sub-Account Management System Design (CW-062)

## Overview

This document describes the sub-account management system implemented in CW-062, enabling master accounts to create and manage subordinate accounts for distributed video replication operations while maintaining centralized billing and resource ownership.

The desktop follow-up (2026-09-21) extends it end-to-end: sub-account passwords
and login admission, the customer self-service lane
(`/api/customer/sub-accounts`), master-wallet resolution on the consumption
path, and the Tauri/React client surfaces (identity badge, management page,
cached display identity).

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

**Session-history pin (029 append-only log)**: `customer_session_events` accepts
only `INSERT` — the append-only trigger refuses `UPDATE`/`DELETE` outright — and
its `user_id` FK carries no CASCADE. An account that ever held a session keeps
its audit trail: any user-row DELETE that would orphan those events (a
sub-account directly, or a master cascading into its sub-rows) fails the FK and
rolls back; the lanes turn that into a graceful answer (degraded deactivation /
409) instead of a 500.

---

## API Endpoints

### Base URL

- **Admin lane**: `/api/admin/sub-accounts` — the operations console, behind
  the CW-026/027 guards (admin session + write CSRF).
- **Customer self-service lane**: `/api/customer/sub-accounts` — a master
  manages its own organisation from the desktop client; a sub-account session
  is answered 403 (see the dedicated section below).

All admin endpoints require admin role authentication.

### 1. Create Sub-Account

**POST** `/api/admin/sub-accounts`

**Request Body**:
```json
{
  "username": "employee_001",
  "display_name": "张三 (员工)",
  "parent_user_id": "master_uuid_here",
  "initial_password": "optional-secret-1",
  "reason": "用于视频批量生产",
  "request_id": "uuid-v4"
}
```

**Validation**:
- `username`: Unique across all users (checked via `SELECT 1 FROM users WHERE username = %s`)
- `parent_user_id`: Must exist AND have `account_type='MASTER'` AND `role='customer'`
- `initial_password` (optional): hashed at the boundary (400 `WEAK_PASSWORD` on
  a policy violation); when present the row is written with
  `registration_source='admin_create'` — the origin the shared password-session
  rule admits. Without it the account exists but cannot hold a session until a
  password is set through the reset endpoint.
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
  "has_password": true,
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

### 4. Reset Sub-Account Password

**POST** `/api/admin/sub-accounts/{sub_account_id}/password`

**Request Body**:
```json
{
  "password": "new-secret-9",
  "reason": "员工换人",
  "request_id": "uuid-v4"
}
```

Setting or rotating the password writes `registration_source='admin_create'`
alongside the hash and revokes the sub-account's live session in the same
transaction — an old session must not outlive the old password. Plaintext never
touches the database or the audit trail.

**Response (200)**:
```json
{ "id": "sub_xyz123", "has_password": true }
```

**Errors**:
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `SUB_ACCOUNT_NOT_FOUND` | 404 | ID doesn't exist or isn't a SUB type |
| `WEAK_PASSWORD` | 400 | Password violates the shared registration policy |

---

### 5. Delete Sub-Account

**DELETE** `/api/admin/sub-accounts/{sub_account_id}`

**Response (200)**:
```json
{
  "deleted": true,
  "sub_account_id": "sub_xyz123"
}
```

**Footprint & history semantics**:
- The session-state/device footprint (`customer_session_state`,
  `customer_devices`) is purged first — those `user_id` FKs carry no CASCADE
- Business history (ledger, tasks) and the append-only session-event log (029)
  are deliberately never deleted: when such rows pin the account the DELETE
  answers 409 and the operator deactivates instead
- Audit log entry created (action: `'sub_account.delete'`)

**Errors**:
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `SUB_ACCOUNT_NOT_FOUND` | 404 | ID doesn't exist |
| `SUB_ACCOUNT_HAS_HISTORY` | 409 | Ledger/task/session history pins the row — deactivate instead |

---

## Customer Self-Service Lane (Desktop Follow-Up)

`/api/customer/sub-accounts` lets a **master** customer run the same lifecycle
from the desktop client without the admin lane:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/customer/sub-accounts` | List the caller's own sub-accounts |
| `POST` | `/api/customer/sub-accounts` | Create a sub-account (optional initial `password`) |
| `PATCH` | `/api/customer/sub-accounts/{id}` | Update `display_name` / `is_active` |
| `POST` | `/api/customer/sub-accounts/{id}/password` | Set/rotate the sub's password |
| `DELETE` | `/api/customer/sub-accounts/{id}` | Delete, or degrade to deactivation |

**Safety rails**:

- Every endpoint rides the customer session fence: no session → 401
  `SESSION_REQUIRED`; a *sub-account* session → 403 `MASTER_ACCOUNT_REQUIRED`.
  Sub-accounts never manage sub-accounts.
- Rows are scoped by `parent_user_id = caller`. A foreign or unknown id is the
  single 404 `SUB_ACCOUNT_NOT_FOUND` — no IDOR oracle distinguishing the two.
- Deactivation (`is_active=false`) and password rotation revoke the sub's live
  session in the same transaction (SES-03 propagation).
- Deactivating the *master* freezes every session riding under it: the shared
  session fence re-checks the parent's `is_active` on every request.

**Create contract** (`POST`): `{username, display_name, password?}` → `201`
with the sub payload (`id, username, display_name, account_type,
parent_user_id, is_active, has_password, created_at, updated_at`). With a
password the sub can log in immediately (the row is written with
`registration_source='admin_create'`); without one the account exists but
cannot hold a session until the master sets a password.

**Delete contract** (`DELETE`) prefers a real delete — the session-state/device
footprint is purged in a savepoint, then the row. When history pins the account
(business rows, or the append-only session-event log), the savepoint rolls back
and the account is deactivated + its session revoked instead:

```json
{ "id": "sub-9f1c...", "deleted": false, "is_active": false }
```

`deleted: true` means the row is gone; `deleted: false` means it survives,
deactivated, until the master deletes it again for a clean account.

**Stable error codes**:

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `SESSION_REQUIRED` | 401 | No customer session token |
| `MASTER_ACCOUNT_REQUIRED` | 403 | Caller is not a MASTER (e.g. a sub session) |
| `SUB_ACCOUNT_NOT_FOUND` | 404 | Foreign or unknown id (single answer) |
| `USERNAME_TAKEN` | 409 | Username already taken |
| `WEAK_PASSWORD` | 400 | Password violates the registration policy |
| `INVALID_DISPLAY_NAME` | 400 | Blank or over 64 characters |
| `EMPTY_UPDATE` | 400 | PATCH with neither field set |

Audit rows land in `audit_logs` under `customer.sub_account.*` with public
metadata only.

---

## Sub-Account Login & Session Admission

Sub-accounts authenticate with a username + password of their own through the
regular customer lane:

- Login (`POST /api/customer/login`, and the legacy password-login alias) and
  the session fence share one admission rule —
  `sub_account_auth.password_login_account_ok`: a sub is admitted only with
  `registration_source='admin_create'`, `account_type IN ('SUB','SUB_ADMIN')`,
  a bound parent, and an **active** master. Masters keep the `self_register` /
  `activation_code` origins.
- The login answer and `GET /api/customer/profile` carry the identity:
  `account_type`, `parent_user_id`, `parent_display_name` — the desktop renders
  the badge from them without an extra admin call.
- Deactivating the master or the sub, or rotating the password, ends the
  session on the next request (fence re-check), not only at lease expiry.

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

### Wallet Resolution (Desktop Follow-Up)

One organisation holds one wallet (the master's). `usage_billing.resolve_wallet_owner`
maps any session user to its wallet owner: a sub resolves to its master; a
master to itself. Consumers:

- Customer wallet reads (`GET /api/customer/wallet`, `/center-summary`,
  `/wallet/transactions`) answer the master's balance for a sub session.
- The consumption chain (accept/finish operations) charges the master's wallet
  while `actor_user_id` keeps the sub's id — spend attribution stays per-person
  on a single organisation balance.
- A sub owns no `wallets` row; top-ups and adjustments always target the master.

---

## Desktop Client Integration (Desktop Follow-Up)

| Layer | Piece | Behavior |
|-------|-------|----------|
| Session | `useCustomerSession` / types | Login + profile responses persist `account_type`, `parent_user_id`, `parent_display_name` into the session state |
| Personal center | `CustomerProfilePanel` | Identity badge (master: "母账号"; sub: "子账号 · 所属母账号") and the sub-account management tab only for masters; unknown identity degrades to no badge |
| Management page | `SubAccountManagementPage` | List / create (optional initial password) / rename / deactivate (with confirmation) / reset password / delete; a `deleted:false` answer surfaces as "已停用（存在历史数据）" instead of a silent no-op |
| Credential cache | Tauri `customer_credentials.rs` | A display-only `StoredCustomerIdentity` copy rides the device credential: a restored session renders the badge on the first frame; renewal keeps it (TS sends `identity: null`), device rotation / logout / clear-all drops it |

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

- `test_sub_account_schema.py`: PG-only schema constraints
- `test_sub_account_crud.py`: admin-lane API integration, including the 409
  refusal when a session history pins the row (029 append-only events)
- `test_customer_sub_account_self_service.py`: customer-lane lifecycle with
  real password sessions — create/login/identity fields, list/rename, single-404
  scope, fence refusal, delete degrade, revocation on deactivate/rotation,
  master deactivation freezing sub sessions, master-wallet reads
- `test_customer_center.py`: profile/session identity contract
- Vitest: `SubAccountManagementPage.test.tsx` (list/create/initial password/
  deactivate confirm/expired session/delete degrade), `CustomerProfilePanel.test.tsx`
  (master badge vs sub badge vs unknown identity)
- Tauri: `customer_credentials.rs` unit tests (identity survives a session
  renewal, dropped on device rotation / clear-all)

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
- [x] Soft-delete path: DELETE now degrades to deactivation whenever history
      (ledger/tasks/append-only session events) pins the row — customer lane
      answers `deleted: false`, admin lane 409 `SUB_ACCOUNT_HAS_HISTORY`
- [ ] Cascading master deletion still assumes no sub holds session history —
      revisit before any bulk org purge ships

---

## References

- [CW-062 Task List](../docs/cw-062-task-list.md)
- [Phase 1 Implementation Notes](../CHANGELOG.md#phase-1-database-foundation)
- [Phase 2 Implementation Notes](../CHANGELOG.md#phase-2-backend-apis)
- [AGENTS.md Docker/Worktree Rules](../../AGENTS.md)