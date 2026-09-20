# 🔍 **日志查询功能设计方案**

**优先级**: P1 - High (运维监控刚需)  
**预计开发时间**: 3-4 个工作日  
**技术栈**: PostgreSQL + ELK Stack (可选)

---

## 🎯 **一、需求分析**

### **业务场景**

| 角色 | 使用场景 | 痛点解决 |
|------|---------|---------|
| **运维工程师** | 快速定位线上故障原因 | ❌ 当前只能查分散的 server logs<br>✅ 统一日志聚合检索 |
| **开发工程师** | Debug 特定用户请求链路 | ❌ 需登录多台服务器 grep<br>✅ Web 界面搜索所有日志 |
| **安全审计** | 追溯异常操作记录 | ❌ 无法完整审计链<br>✅ 按用户/IP/时间多维查询 |
| **产品经理** | 验证功能上线效果 | ❌ 看不到实时用户行为<br>✅ 过滤活跃事件追踪 |

---

## 🔧 **二、技术方案设计**

### **架构选型对比**

#### **方案 A: 轻量级 PostgreSQL-only (推荐用于 M2)** ⭐

**优点**:
- ✅ 利用现有数据库基础设施
- ✅ 零额外依赖部署
- ✅ SQL 查询能力强大（JOIN/GROUP）
- ⚠️ 实时写入性能中等
- ⚠️ 数据保留期较短（需定期清理）

**适用场景**: 
- 日日志量 <100MB
- 数据保留 30 天
- 并发查询 <50 QPS

---

#### **方案 B: Full ELK Stack (Elasticsearch + Logstash + Kibana)** ⭐⭐

**优点**:
- ✅ 高性能全文检索
- ✅ 支持 PB 级数据存储
- ✅ 丰富的可视化 Dashboard
- ✅ TTL 自动删除策略
- ❌ 资源消耗大（至少 4GB RAM）
- ❌ 运维复杂度提升

**适用场景**:
- 日日志量 >500MB
- 数据保留 90+ 天
- 需要复杂聚合分析

---

#### **方案 C: Loki + Grafana (云原生轻量替代)** ⭐⭐⭐

**混合优势**:
- ✅ 比 ELK 更轻量（只索引 metadata）
- ✅ Grafana 开箱即用
- ✅ 与 Prometheus 无缝集成
- ✅ 成本低（PostgreSQL backend)

**推荐指数**: ★★★★☆ (最佳平衡点)

---

### **本文采用方案 C: Loki + Grafana 架构**

```mermaid
flowchart LR
    subgraph "Application Layer"
        App[FastAPI App] -->|log.info()| Logger
    end
    
    subgraph "Ingestion Layer"
        Logger[Loki Log Context] -->|gRPC| Agent[Docker Container]
        Agent -->|push| Gateway[Loki Query API]
    end
    
    subgraph "Storage Layer"
        Gateway --> Store[MinIO / S3 Bucket]
    end
    
    subgraph "Query Layer"
        Grafana[Grafana UI] -->|query| Gateway
    end
```

---

## 🛠️ **三、核心实现细节**

### **Phase 1: Logging Infrastructure Setup**

#### **1. 应用日志改造**

```python
# server/app/logging_config.py
import structlog
from datetime import datetime

def configure_logging(level="INFO", format_type="json"):
    """Configure structured logging with context extraction."""
    
    common_processors = [
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Custom: Add request context
        add_request_context,
        # Custom: Add user context if authenticated
        add_user_context,
    ]
    
    if format_type == "json":
        structlog.configure(
            processors=[*common_processors, structlog.processors.JSONRenderer()],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[*common_processors, structlog.dev.ConsoleFormatter()],
            wrapper_class=structlog.make_filtering_bound_logger(level),
        )

def add_request_context(event_dict):
    """Extract request_id from headers if present."""
    request_id = getattr(structlog.threadlocal.get(), 'request_id', None)
    if request_id:
        event_dict['request_id'] = request_id
    return event_dict

def add_user_context(event_dict):
    """Add current user_id if authenticated."""
    try:
        from app.auth import get_current_user
        user = get_current_user()
        if user:
            event_dict['user_id'] = str(user.id)
    except Exception:
        pass  # No user session
    return event_dict
```

**Usage in code**:
```python
# Before: Plain print/log
logger = logging.getLogger(__name__)
logger.error(f"Failed to download video {video_id}")

# After: Structured logging with context
import logging
log = structlog.get_logger()

try:
    await download_video(video_id)
except Exception as e:
    log.error(
        "video_download_failed",
        video_id=video_id,
        error=str(e),
        exc_info=True,  # Include traceback
        user_id=getattr(current_user, 'id', 'anonymous')
    )
```

---

#### **2. Middleware Request ID Propagation**

```python
# server/app/middleware/request_id_middleware.py
from uuid import uuid4
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Generate or extract request ID
        request_id = request.headers.get("x-request-id") or str(uuid4())
        
        # Attach to ASGI scope for downstream access
        request.state.request_id = request_id
        
        # Add header to response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        
        return response
```

**Integration**:
```python
# server/app/main.py
from app.middleware.request_id_middleware import RequestIDMiddleware

app.add_middleware(RequestIDMiddleware)
```

---

### **Phase 2: Loki Integration**

#### **1. Docker Compose Configuration**

```yaml
# docker-compose.logging.yml
version: "3.8"

services:
  loki:
    image: grafana/loki:2.9.0
    container_name: loki
    volumes:
      - ./loki-config.yaml:/etc/loki/loki-config.yaml
      - ./logs:/var/lib/loki/logs
    command: -config.file=/etc/loki/loki-config.yaml
    ports:
      - "3100:3100"  # Query API
    networks:
      - logging-net
    restart: unless-stopped
    
  promtail:
    image: grafana/promtail:2.9.0
    container_name: promtail
    volumes:
      - ./promtail-config.yaml:/etc/promtail/config.yaml
      - ./logs:/var/log/application:ro
    command: -config.file=/etc/promtail/config.yaml
    depends_on:
      - loki
    networks:
      - logging-net
    restart: unless-stopped
      
  grafana:
    image: grafana/grafana:10.2.0
    container_name: grafana
    environment:
      - GF_INSTALL_PLUGINS=grafana-lokiexplore-app
    ports:
      - "3001:3000"
    networks:
      - logging-net
    restart: unless-stopped

networks:
  logging-net:
    driver: bridge
```

---

#### **2. Loki Config File**

```yaml
# loki-config.yaml
auth_enabled: false

server:
  http_listen_port: 3100
  grpc_listen_port: 9096

schema_config:
  configs:
    - from: 2024-01-01
      store: boltdb-shipper
      object_store: filesystem
      schema: v11
      index:
        pool:
          max_idle_conns: 100

storage_config:
  boltdb_shipper:
    active_index_directory: /var/lib/loki/index
    cache_location: /var/lib/loki/cache
    shared_store: filesystem
  filesystem:
    directory: /var/lib/loki/chunks

limits_config:
  enforce_metric_name: false
  rejection_limit_message_size_mb: 50
  ingestion_rate_mb: 10
  ingestion_burst_size_mb: 20
  retention_period: 30d  # Auto-delete logs older than 30 days

chunk_store_config:
  read_index_cache_ttl: 1h

table_manager:
  retention_deletion_policy:
    - name: 30day-retention
      type: retention
      retention_period: 720h  # 30 days
  retention_deletion_poll_interval: 5m

ruler:
  alertmanager_url: http://alertmanager:9093
```

---

### **Phase 3: Grafana Dashboard Creation**

#### **1. Log Stream Viewer Panel**

```json
{
  "dashboard": {
    "title": "Real-time Application Logs",
    "panels": [{
      "id": 1,
      "type": "logs",
      "title": "Live Logs",
      "datasource": "Loki",
      "targets": [{
        "expr": "{job=\"application\"} |= \"ERROR\"",
        "refId": "A",
        "limit": 500
      }],
      "options": {
        "showLabels": true,
        "showTime": true,
        "wrapLogMessage": true,
        "prettifyLogMessage": true
      }
    }],
    "timeRange": {
      "from": "now-15m",
      "to": "now"
    }
  }
}
```

---

#### **2. Advanced Search Query Example**

```sql
-- Query: Find all errors related to user John Smith in last hour
{job="application"} |= "error" 
| json 
| user_email="john.smith@example.com" 
| __line__=~".*video_download.*" 
| last 1h

-- Result: Shows exact line numbers and stack traces
```

---

## ✅ **四、验收标准**

| ID | 测试项 | 预期结果 | 工具 |
|----|--------|---------|------|
| LV-01 | 日志结构化 | JSON 格式可解析 | `jq .` validation |
| LV-02 | Request ID 传递 | Header 全程携带 | Postman trace |
| LV-03 | Loki 接收延迟 | <5s 从生成到可查询 | Real-time test |
| LV-04 | Grafana 刷新 | 每 30s 自动更新 | Browser automation |
| LV-05 | 多租户隔离 | 不同 job 标签数据独立 | Label query test |
| LV-06 | Retention 策略 | 30 天后自动删除 | Cron schedule verify |
| LV-07 | 权限控制 | Only admin role can view | RBAC test |

---

## 📋 **五、开发任务分解**

```markdown
Day 1: Core Implementation
- [ ] Install Loki + Promtail + Grafana on staging server
- [ ] Modify app logging config to use structlog + JSON format
- [ ] Implement RequestID middleware
- [ ] Add context enrichment functions

Day 2: Integration Testing
- [ ] Create Grafana dashboards (Live View / Search / Aggregation)
- [ ] Configure retention policies in Loki
- [ ] Set up Prometheus metrics collection

Day 3: Documentation & Rollout
- [ ] Write user manual for operations team
- [ ] Create runbook for common troubleshooting queries
- [ ] Deploy to production (canary release first)

Day 4: Optimization
- [ ] Performance tuning for high-volume scenarios
- [ ] Add custom alerts (CPU spikes / Error rate increase)
- [ ] Fine-tune retention policy based on actual usage
```

---

## 💡 **六、扩展功能规划**

### **Advanced Features (Optional)**

1. **Custom Alert Rules**:
   ```yaml
   rules:
     - alert: HighErrorRate
       expr: rate({job="application"} |= "error"[5m]) > 0.5
       for: 2m
       labels:
         severity: critical
       annotations:
         summary: "High error rate detected"
   ```

2. **Log Sampling Strategy**:
   ```python
   # Sample 1% of INFO logs to reduce storage cost
   if level == "info" and random.random() > 0.99:
       return  # Skip logging
   
   # Always log ERROR/WARN regardless of sample ratio
   ```

3. **Multi-source Correlation**:
   ```sql
   -- Join application logs with database slow queries
   ({job="application"} |= "slow_query") 
   OR 
   ({job="postgres"} |= "duration" | duration > "1s")
   ```

---

## 📊 **七、成本估算**

### **Infrastructure Costs**

| Component | Resource | Monthly Cost |
|-----------|----------|-------------|
| Loki Server | 2 vCPU, 4GB RAM | ~¥200 (self-hosted) |
| Storage | 50GB SSD | ~¥100 |
| Grafana | Same as Loki | Included |
| **Total** | | **~¥300/month** |

### **Comparison with Cloud Services**

| Service | Monthly Price | Notes |
|---------|--------------|-------|
| Self-hosted Loki | ~¥300 | Most economical |
| AWS CloudWatch | ~¥2000 | Pay-per-ingestion |
| Datadog Logs | ~¥5000/million events | Premium feature set |
| ELK Cloud | ~¥3000 | Enterprise SLA |

**Recommendation**: Start with self-hosted for MVP, scale to managed service when needed.

---

**请确认是否采用此方案？预计需要 3-4 个工作日完成基础版开发。**
