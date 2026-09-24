# 决策记录：微信支付 V3 Native 客户端 SDK 选型

> 说明：本仓库中的 CW-/T- 编号指向项目内部的任务跟踪系统，对应的任务台账与验收证据不随本仓库发布。

## Status

**Accepted** — 本决策对应的自实现方案已落地并通过全量静态门禁与单测（50 passed / mypy strict / ruff 全绿）。

## Context

### 任务范围

CW-069 要求交付「微信支付 V3 Native 客户端封装层」（纯服务层，零迁移），核心能力：

- `POST /v3/pay/transactions/native` 下单 → 返回 `code_url`
- SHA256-RSA 商户私钥请求签名 + 应答验签
- 平台证书自动下载 / 内存缓存 / TTL 轮换 + AES-256-GCM 解密
- `WeChatNativeProvider` conform CW-066 `PaymentProvider` Protocol 并 `register_provider("wechat_native")`

### 选型问题

微信支付 V3 的签名、验签、平台证书轮换是安全关键且协议细节繁琐的部分。摆在面前的两条路：

1. **引入社区 SDK `wechatpayv3`**（minibear2021/wechatpayv3，MIT，PyPI 发布）——开箱即用，覆盖 V3 全量能力。
2. **基于 `cryptography` + 标准库 `urllib` 自实现**——只覆盖 CW-069 所需的 Native 下单 / 查单 / 证书轮换。

### 初始假设（来自 CW-065 技术方案 Phase2 line283）

任务来源文档提出：**`wechatpayv3` SDK 可能与项目的 `cryptography==50.0.0` 硬 pin 存在依赖冲突风险**，并据此倾向于自实现。本决策记录对该假设做了实证核查（见下节），并给出最终的真实理由。

## 依赖事实核查（关键）

对「pin 冲突」假设做了双向核查，取两侧权威声明：

| 依赖 | 项目侧 pin（`server/pyproject.toml`） | wechatpayv3 v2.0.4 约束（GitHub `setup.py`） | 是否冲突 |
|------|--------------------------------------|---------------------------------------------|---------|
| Python | `requires-python = ">=3.12"` | `python_requires=">=3.10"` | 否（3.12 满足 ≥3.10） |
| cryptography | `cryptography==50.0.0`（硬 pin） | `cryptography>=35.0.0`（仅下界，无上限） | **否**（50.0.0 ≥ 35.0.0） |
| requests | `requests>=2.31.0`（已在依赖树） | `requests>=2.21.0` | 否（2.31.0 ≥ 2.21.0，且 requests 本就存在） |

**核查结论：初始假设被证伪。** wechatpayv3 v2.0.4 对 `cryptography` 只声明了下界 `>=35.0.0`，项目的 `==50.0.0` 完全落在其允许区间内；`requests` 也已是项目现有依赖（并非新增传递依赖）。因此 wechatpayv3 v2.0.4 **可以与当前 pin 干净共存，不存在硬版本冲突**。

> 诚实记录：选型结论（自实现）保持不变，但**支撑理由必须从「依赖冲突」修正为下节的真实工程理由**。一个基于错误事实的决策记录会误导后续维护者。

## Decision

**选择方案 2：基于 `cryptography==50.0.0` + 标准库 `urllib` 自实现，不引入 `wechatpayv3`。**

决策**不**依赖「pin 冲突」这一已被证伪的理由，而基于以下可验证的工程考量。

## 决策理由（真实理由）

### 1. 与既有 `zpay.py` 客户端模式保持一致（最强理由）

仓库已有一个同层级的支付客户端蓝本 `server/app/zpay.py`（CW-066 交付），其确立的模式为：

- 标准库 `urllib.request` 发起 HTTP，**不引入第三方 HTTP 栈**；
- `ZPayHTTPOpener` Protocol + `opener or cast(.., urlopen)` 的**依赖注入**；
- 显式异常分类：`HTTPError → 502`，`TimeoutError/URLError/OSError → 504`；
- 应答大小上限（size cap）+ `sha256` response digest。

`wechat_native_client.py` 完整复刻了该模式（`WeChatHTTPOpener` / `WeChatHTTPResponse` Protocol、同款异常分类、`MAX_WECHAT_RESPONSE_BYTES = 256 * 1024`、`response_digest`）。引入 wechatpayv3 会让两个并列的 Provider 走上**两套不同的 HTTP 与错误处理栈**，破坏一致性、抬高维护心智负担。

### 2. 零网络单测可测性

得益于 mock opener 注入，CW-069 的 **50 个单测全部用自生成测试 RSA 密钥对 + mock HTTP 响应运行，绝不触网、绝不联调真实微信商户号/私钥/api_v3_key**，也不需要 `pg` marker。wechatpayv3 在其内部自行完成网络 I/O 与证书管理，要在隔离环境测它就得 monkeypatch 其内部实现或依赖 live sandbox——测试保证更弱，且与仓库「纯单元、零外部依赖」的测试纪律不符。

### 3. 安全关键逻辑完全可控

请求签名、应答验签、平台证书 AES-256-GCM 解密与 TTL 轮换是安全关键路径，必须匹配本项目的策略：

- 平台证书缓存 TTL 明确为 `CERTIFICATE_CACHE_TTL_SECONDS = 12 * 60 * 60`（12h）；
- 超时 `WECHAT_TIMEOUT_SECONDS = 10.0`；
- 证书信任根为 `api_v3_key`（AES-GCM auth tag），**无需先验签**，规避「验签需证书、证书需下载」的鸡生蛋问题；
- 应答缺失验签 header 一律 `fail-closed`（抛 `WeChatSignatureError`）。

自实现让这些策略全部显式、可审计、可单测覆盖；SDK 的内部证书缓存/轮换策略是黑盒，且会随其版本漂移。

### 4. 供应链与维护面

微信支付官方未提供一等公民 Python SDK（官方 SDK 主要覆盖 Java/PHP 等），Python 生态以社区库为主。`wechatpayv3` 由单一社区维护者发布，把支付关键路径耦合到第三方发布节奏会扩大供应链面。自实现只依赖**已被 pin 的 `cryptography` + 标准库**，无新增外部运行时依赖。

### 5. 范围贴合

CW-069 只需 Native 下单 + 查单 + 平台证书轮换。wechatpayv3 是覆盖全部支付方式、退款、转账、分账、回调的全量 SDK，远超所需表面积。自实现 `wechat_native_client.py`（581 行）+ `wechat_native_provider.py`（177 行）精确覆盖需求，无冗余。

## Consequences

### 正面影响

- ✅ 与 `zpay.py` 同构，Provider 层心智模型统一；
- ✅ 50 个单测零网络、零 DB、零真实凭据，CI 可稳定复现；
- ✅ 签名/验签/证书轮换策略显式可控、可审计；
- ✅ 无新增第三方运行时依赖（仅用已 pin 的 `cryptography` + stdlib）；
- ✅ 通过 mypy strict（`server/app`）+ ruff（`server` 全量，含 tests）全绿。

### 风险与成本

- ⚠️ **自维护协议逻辑**：签名消息拼装、验签、AES-GCM 解密、证书轮换需自己维护（~758 行实现），微信若变更 V3 协议细节需自行跟进——这是自实现相对 SDK 的固有成本。
- ⚠️ **能力面窄**：仅覆盖 Native 下单/查单/证书轮换；后续若要接入退款、转账、JSAPI/H5 等，需要各自扩展（届时可重新评估是否引入 SDK）。
- ⚠️ **回调验签未在本任务内实现**：见下节 fail-closed 说明，属路由级后续任务。

## Alternatives Considered

### Alternative 1：引入 `wechatpayv3` SDK

**Pros**：开箱即用，覆盖 V3 全量能力，签名/证书轮换由 SDK 内部处理，减少自维护代码。

**Cons**：
- 与既有 `zpay.py`（stdlib urllib + mock opener）模式分裂，两套 HTTP/错误栈并存；
- 内部自行网络 I/O，隔离单测需 monkeypatch 其内部或依赖 live sandbox，测试保证弱；
- 证书缓存/轮换等安全策略为黑盒且随版本漂移，难以匹配本项目 12h TTL / fail-closed 要求；
- 耦合单一社区维护者的发布节奏；
- 能力远超 CW-069 所需（全量支付/退款/转账/分账），表面积冗余。

> 注：**「与 `cryptography==50.0.0` 冲突」不在 Cons 之列**——依赖核查已证伪该点（v2.0.4 仅要求 `cryptography>=35.0.0`）。

### Alternative 2：等待/采用微信支付官方 Python SDK

**Pros**：官方背书，协议跟进及时。

**Cons**：微信支付官方并未提供一等公民 Python SDK，此路当前不通。

## 相关实现层决策（一并记录）

以下决策在实现中确立，与选型同源，记录以备后续任务对齐：

1. **`code_url` 双字段映射**：微信 Native 下单只返回 `code_url`（`weixin://wxpay/bizpayurl?pr=...` 形式的二维码**内容串**，而非图片 URL）。故 `create_payment_code` 将 `PaymentCodeResult.qr_image_url` 与 `payment_url` **均设为 `code_url`**，`provider_order_no=None`。前端渲染二维码由消费方负责（属 CW-072 客户端任务）。

2. **`create_payment_form` 不支持**：微信 Native 无表单支付形态，`create_payment_form` 显式抛 `PaymentProviderError`，引导调用方走 `create_payment_code`。

3. **`verify_notification` fail-closed**：微信回调验签需要原始 raw body + 应答 header + AES-GCM 解密，无法从 Protocol 约定的 `Mapping[str, str]` 重建；故本层 `verify_notification` 返回 `valid=False, error_code=WECHAT_NATIVE_CALLBACK_UNSUPPORTED`（fail-closed，绝不默认放行）。真正的回调验签属路由级后续任务（CW-070 回调路由）。

4. **零迁移边界**：复用现有 `provider_settings` 表 + `SettingsRepository.load_provider_config("wechat_native")` 读取商户配置，**CW-069 不新增任何 alembic 迁移**；微信商户配置 UI 与 `save/validate_provider_config` 属 CW-071（迁移 085）。

5. **异常分类**（复刻 zpay.py）：`HTTPError → 502`，`TimeoutError/URLError/OSError → 504`；`HTTPError` 必须在 `URLError/OSError` **之前**捕获（`HTTPError ⊂ URLError ⊂ OSError` 的子类顺序）。

## Implementation Notes

### CW-069 交付物

- [x] `server/app/wechat_native_client.py`（581 行）— V3 Native HTTP 客户端：加密原语 / 配置层 / native 下单 / 应答验签 / 平台证书下载+缓存+TTL 轮换 / AES-256-GCM 解密 / `WeChatHTTPOpener` 依赖注入
- [x] `server/app/wechat_native_provider.py`（177 行）— `WeChatNativeProvider` conform `PaymentProvider` Protocol + `register_provider("wechat_native")`
- [x] `server/tests/test_wechat_native_client.py`（793 行）— 签名/验签/证书轮换/native 下单/查单 mock 单测（自生成测试 RSA 密钥 + mock opener）
- [x] `server/tests/test_wechat_native_provider.py`（262 行）— Protocol 契约 + registry + 映射/fail-closed 测试
- [x] `docs/decisions/CW-069-wechat-sdk-selection.md` — 本决策记录

### 验证证据（AUTOMATED_VERIFIED）

- `pytest tests/test_wechat_native_client.py tests/test_wechat_native_provider.py` → **50 passed**
- `mypy --config-file server/pyproject.toml server/app` → **Success**（strict）
- `ruff check server` + `ruff format --check server` → **All checks passed / already formatted**

> 按 claim.json `evidence_tier_policy`：**不声称已联调真实微信支付**（无生产商户密钥），**不声称迁移验证**（本任务零迁移）。所有单测用自生成测试 RSA 密钥对 + mock HTTP 响应。

## References

- 项目依赖 pin：`server/pyproject.toml`（`cryptography==50.0.0`, `requests>=2.31.0`, `requires-python>=3.12`）
- wechatpayv3 v2.0.4 依赖声明：`github.com/minibear2021/wechatpayv3` → `setup.py`（`python_requires>=3.10`, `cryptography>=35.0.0`, `requests>=2.21.0`）
- 客户端蓝本：`server/app/zpay.py`
- Provider Protocol：`server/app/payment_provider.py`
- 微信支付 API v3（Native 下单 / 平台证书 / 签名规范）：pay.weixin.qq.com 官方文档

---

*Created: 2026-09-12*
*Maintained by: 项目组*
