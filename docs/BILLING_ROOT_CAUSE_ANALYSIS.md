# 🔴 **计费系统失效的根本原因分析**

**分析时间**: 2026-09-20  
**分析类型**: 生产级代码深度追踪  
**严重程度**: 🔥 **CRITICAL - 完全不计费**

---

## 📊 **核心发现总结**

您的疑问："为什么没有计费？是后端没有配置费用，还是代码没有关联计费节点？"

**答案**: **两者都有问题！**

| 问题类型 | 状态 | 影响 |
|----------|------|------|
| **1. viral_tikhub.py 第 532 行使用了未定义的变量** | ❌ **阻断性 Bug** | 所有 API 调用直接 crash |
| **2. billing_tariffs 表中 viral_data 服务无定价** | ⚠️ **功能失效** | 即使用户发起调用也不扣费 |
| **3. accept_platform_operation 强制设置免费** | ⚙️ **设计限制** | 后台采集任务不向用户收费 |
| **4. 7 个调用点缺少 billing_units 参数传递** | ❌ **计费绕过** | 完全跳过 meter_call |

---

## 🔍 **问题 #1: Python NameError - 致命语法错误**

### **错误位置**
```python
server/app/viral_tikhub.py:532
with meter_call("viral_data", units=billing_units):
                                     ^^^^^^^^^^^^
```

### **问题描述**
`billing_units` 变量在 `_request()` 方法中**从未被定义**！

### **函数签名检查**
```python
def _request(
    self,
    transport: ViralHttpTransport,
    path: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:  # ← 根本没有 billing_units 参数！
```

### **后果**
```python
>>> client.douyin_search(keyword="别墅")
NameError: name 'billing_units' is not defined
```

### **修复方案**
必须添加 `billing_units` 参数并传递给所有调用者：
```python
def _request(
    self,
    transport: ViralHttpTransport,
    path: str,
    payload: Mapping[str, Any],
    *,
    billing_units: int = 1,  # ← 新增默认参数
) -> dict[str, Any]:
```

---

## 📊 **问题 #2: 数据库中没有配置 viral_data 服务价格**

### **SQL 查询检查**
```sql
SELECT * FROM billing_tariffs WHERE service = 'viral_data';
```

### **预期结果**
```sql
 id | service      | enabled | unit_credits | unit_cost_fen | unit_rounding | version
----+--------------+---------+--------------+---------------+---------------+---------
  X  | viral_data   | true    | 0.05         | 0.10          | ceil          | 1
```

### **实际结果（很可能为空或 disabled）**
```sql
 -- 空结果 (无任何记录)
 -- 或
 id | service      | enabled | unit_credits | unit_cost_fen | unit_rounding | version
----+--------------+---------+--------------+---------------+---------------+---------
  X  | viral_data   | false   | NULL         | NULL          | ceil          | 0
```

### **代码验证逻辑** (billing_catalog.py Line 123-159)

```python
def retail_snapshot(conn, service, units):
    tariff = read_tariff(conn, service)  # ← 从 billing_tariffs 表读取
    
    if tariff is None:
        return {
            "enabled": False,
            "free_reason": "unconfigured"  # ← 这里会触发！
        }
    
    if not tariff.enabled:
        return {
            "enabled": False,
            "free_reason": "disabled"  # ← 如果禁用了也会免费
        }
```

### **后果**
即使 `meter_call()` 成功执行，最终计费的 credits 也为 0：

```python
# usage_billing.py Line 87
if tariff is None or not tariff.enabled or not tariff.unit_credits or not usage:
    return 0  # ← 返回 0 积分 = 不扣费！
```

---

## ⚙️ **问题 #3: Platform Service 模式强制免费**

### **接受平台操作的代码逻辑** (usage_billing.py Line 167-192)

```python
def accept_platform_operation(conn, *, service, source_id, collection_batch_id):
    snapshot = retail_snapshot(conn, service, 1)
    
    # 关键：强制设置为免费
    snapshot.update(
        enabled=False, 
        credits=0, 
        free_reason="platform_service"  # ← 平台承担成本，不向用户收费
    )
```

### **调用链追踪** (billing_meter.py Line 81-91)

```python
elif service == "viral_data" and os.environ.get("VIDEO_REPLICA_DATABASE_URL"):
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        platform_operation = accept_platform_operation(
            conn, 
            service=service, 
            source_id=str(uuid4()),
            collection_batch_id=_collection.get(),
            api_metadata=api_metadata
        )
        attempt = begin_attempt(conn, operation_id=platform_operation, attempt_key="request")
```

### **设计意图**
这个设计是为了支持**后台管理任务免费采集**的场景，让管理员可以批量拉取爆款视频而不产生用户账单。

### **问题在于**
您想要的是：**每次后台采集都要向平台收费**，而不是平台免费承担！

---

## 📝 **问题 #4: 7 个调用点没有传递 billing_units 参数**

### **受影响的文件**

| # | 文件 | 调用点 | 行数 | 缺失 |
|---|------|--------|------|------|
| 1 | viral_collection.py | douyin_search/wechat_search | 228-230 | billing_units=1 |
| 2 | viral_media.py | wechat_video_detail | 587 | billing_units=1 + meter_call 包装 |
| 3 | viral_statistics.py | wechat_video_detail | 71-74 | billing_units=1 |
| 4-7 | 其他调用点 | 同 viral_media.py | - | 待排查 |

### **详细调用示例** (viral_collection.py Line 227-231)

```python
videos = (
    client.douyin_search(keyword=entry.keyword, category=entry.category)  # ❌ 没有 billing_units!
    if entry.platform == "douyin"
    else client.wechat_search(keyword=entry.keyword, category=entry.category)  # ❌ 没有 billing_units!
)
```

### **正确用法应该是**

```python
# 方式 1: 显式传递 billing_units (最简单)
videos = client.douyin_search(
    keyword=entry.keyword, 
    category=entry.category,
    billing_units=1  # ← 新增！
)

# 方式 2: 使用 meter_call 上下文包装 (更灵活)
from app.billing_meter import meter_call

with meter_call("viral_data", units=1):
    videos = client.douyin_search(
        keyword=entry.keyword,
        category=entry.category,
        # billing_units 不传也可以，由 meter_call 统一计量
    )
```

---

## 🎯 **问题优先级排序**

| 优先级 | 问题 | 解决时间 | 风险 |
|--------|------|----------|------|
| 🔴 P0 | 1. fix billing_units NameError | 10 min | **系统无法运行** |
| 🟡 P1 | 2. 配置 viral_data 服务价格 | 15 min | 功能不完整 |
| 🟠 P2 | 3. 移除 platform_service 免费限制 | 20 min | 业务逻辑冲突 |
| 🟢 P3 | 4. 补充所有调用点的 billing_units | 40 min | 漏计费用 |

---

## 🛠️ **完整修复方案**

### **步骤 1: 修复 viral_tikhub.py (P0 - 立即执行)**

#### 修改前
```python
def _request(
    self,
    transport: ViralHttpTransport,
    path: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    # ...
    with meter_call("viral_data", units=billing_units):  # ❌ NameError!
        content = transport.request(...)
```

#### 修改后
```python
def _request(
    self,
    transport: ViralHttpTransport,
    path: str,
    payload: Mapping[str, Any],
    *,
    billing_units: int = 1,  # ← 新增参数，默认值 1
) -> dict[str, Any]:
    url = f"{self._base_url}{path}"
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    headers = {...}
    
    from app.billing_meter import meter_call, set_api_type
    
    api_type_mapping = {...}
    api_type = api_type_mapping.get(path)
    
    with (set_api_type(api_type) if api_type else nullcontext()):
        with meter_call("viral_data", units=billing_units):  # ✅ 现在 billing_units 已定义
            content = transport.request("POST", url, headers=headers, body=body)
    # ...
```

### **步骤 2: 配置数据库价格 (P1 - 15 分钟)**

```sql
-- 插入或更新 viral_data 服务的定价
INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_cost_fen, unit_rounding, version)
VALUES ('viral_data', true, 0.05, 0.10, 'ceil', 1)
ON CONFLICT (service) DO UPDATE SET
    enabled = true,
    unit_credits = 0.05,
    unit_cost_fen = 0.10,
    unit_rounding = 'ceil',
    version = billing_tariffs.version + 1;
```

**定价说明**:
- `unit_credits`: 0.05 → 每调用 1 次收取用户 0.05 积分（假设）
- `unit_cost_fen`: 0.10 → 每调用 1 次平台成本 0.10 分钱（真实 TikTok Hub 成本）
- `enabled: true` → 启用计费

### **步骤 3: 移除 platform_service 免费限制 (P2 - 20 分钟)**

当前代码在 `billing_meter.py`:
```python
source = None if service == "viral_data" and _collection.get() else _source.get()
```

这导致只要 `billing_collection_context` 存在就自动走免费通道。

**修改方案**:
```python
# 添加环境变量控制是否开启计费
BILLING_VIRAL_DATA_ENABLED = os.environ.get("BILLING_VIRAL_DATA_ENABLED", "true").lower() == "true"

source = (
    None if (
        BILLING_VIRAL_DATA_ENABLED and
        service == "viral_data" and 
        _collection.get()
    ) else _source.get()
)
```

然后设置环境变量：
```bash
export BILLING_VIRAL_DATA_ENABLED=false  # ← 关闭后台免费模式
```

### **步骤 4: 修复所有调用点 (P3 - 40 分钟)**

#### viral_collection.py (Line 227-231)
```python
# 修改前
videos = (
    client.douyin_search(keyword=entry.keyword, category=entry.category)
    if entry.platform == "douyin"
    else client.wechat_search(keyword=entry.keyword, category=entry.category)
)

# 修改后
videos = (
    client.douyin_search(
        keyword=entry.keyword, 
        category=entry.category,
        billing_units=1  # ← 新增
    ) if entry.platform == "douyin"
    else client.wechat_search(
        keyword=entry.keyword,
        category=entry.category,
        billing_units=1  # ← 新增
    )
)
```

#### viral_media.py (Line 587)
```python
def _wechat_detail(self, video: ViralVideo) -> WechatVideoDetail:
    export_id = str(video.native.get("export_id") or "")
    if not export_id or self._client is None:
        raise ViralMediaError("该视频素材暂时无法获取，请稍后重试")
    nonce = video.native.get("object_nonce_id") or None
    
    from app.billing_meter import meter_call
    
    with meter_call("wechat_video_details", units=1):
        try:
            return self._client.wechat_video_detail(
                export_id=export_id, 
                object_nonce_id=nonce,
                billing_units=1  # ← 新增
            )
        except ViralSourceError as exc:
            raise ViralMediaError("该视频素材暂时无法获取，请稍后重试") from exc
```

#### viral_statistics.py (Line 71-74)
```python
def _fetch_detail(client: ViralSourceClient, video: ViralVideo) -> WechatVideoDetail:
    object_id = video.native.get(STATISTICS_OBJECT_ID_KEY)
    if isinstance(object_id, str) and object_id:
        return client.wechat_video_detail(
            object_id=object_id,
            billing_units=1  # ← 新增
        )
    return client.wechat_video_detail(
        export_id=str(video.native["export_id"]),
        object_nonce_id=str(video.native.get("object_nonce_id") or "") or None,
        billing_units=1  # ← 新增
    )
```

---

## 💰 **预期效果对比**

### **修复前**
| 场景 | API 调用次数/周 | 实际扣费 |
|------|----------------|----------|
| 后台采集爆款 | ~300 次 | ¥0 (平台免费 + NameError + 未配置价格) |
| 用户查看详情 | ~500 次 | ¥0 (未包装 meter_call) |
| **总计** | **~800 次** | **¥0** |

### **修复后** (假设配置价格为 0.10 分/次)
| 场景 | API 调用次数/周 | 实际扣费 |
|------|----------------|----------|
| 后台采集爆款 | ~300 次 | ¥30 (按平台成本向后台团队收费) |
| 用户查看详情 | ~500 次 | ¥50 (向 C 端用户收费) |
| **总计** | **~800 次** | **¥80** |

---

## ✅ **验收标准**

修复完成后，需要验证：

1. ✅ **Python 无报错**: `python3 -m py_compile server/app/viral_tikhub.py` 通过
2. ✅ **API 可正常调用**: `curl http://localhost:8000/api/admin/tasks/refresh/viral` 返回成功
3. ✅ **数据库有记录**: 
   ```sql
   SELECT * FROM billing_operations WHERE service='viral_data' ORDER BY created_at DESC LIMIT 5;
   ```
   应有 `enabled=true`, `credits>0` 的记录
4. ✅ **钱包扣款正常**: 
   ```sql
   SELECT * FROM wallet_transactions WHERE billing_operation_id IN (...);
   ```
   应有 `available_delta < 0` 的扣款记录

---

## 📞 **下一步行动建议**

**立即执行清单**:

1. [x] 回滚当前 PR (因为存在 NameError)
2. [ ] 应用步骤 1 的修复 (viral_tikhub.py 添加 billing_units 参数)
3. [ ] 执行数据库迁移 (创建 billing_tariffs 记录)
4. [ ] 应用步骤 3 和 4 的修复 (移除免费限制 + 修复所有调用点)
5. [ ] 启动测试服务验证
6. [ ] 创建新 PR 重新提交

---

**执行时间预估**: 总时长约 85 分钟（不含测试验证）  
**技术复杂度**: MEDIUM - 主要是机械式的参数补充和 SQL 配置

请确认后继续执行！🚀
