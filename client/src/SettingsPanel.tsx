import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type BillingSettings,
  getSettings,
  type ProviderName,
  type ProviderSettings,
  type ProviderTestResult,
  type RuntimeSettings,
  revealProviderSecret,
  type SettingsSnapshot,
  testProviderConnection,
  updateBillingSettings,
  updateProviderSettings,
  updateRuntimeSettings,
} from "./api";

/**
 * 设置面板的数据后端。工作台面（`/api/admin/settings`）由本模块内置；
 * 控制面（`/api/control/settings`，内部通道 + 反代令牌）必须由管理端调用方
 * 注入，本模块不静态引用任何 `*Control*` API。
 *
 * 这样客户构建制品的依赖图不含控制面代码——`scripts/verify_customer_bundle.mjs`
 * 对 `client/dist` 断言 `/api/control/` 与 `X-Control-Proxy-Token` 零命中。
 * 构建层 tree-shake 无法替代本设计：客户入口经
 * `CustomerWorkspace → StudioWorkspace → SettingsPanel` 静态复用本组件，
 * 组件内的 `source === "control"` 运行时三元分支会让打包器保留两侧引用。
 */
export type SettingsBackend = {
  load: () => Promise<SettingsSnapshot>;
  saveProvider: (
    provider: ProviderName,
    config: Record<string, string>,
  ) => Promise<ProviderSettings>;
  saveRuntime: (runtime: RuntimeSettings) => Promise<RuntimeSettings>;
  saveBilling: (input: {
    internal_base_unit_price_fen: number;
    oral_unit_price_fen: number;
    min_recharge_fen: number;
    recharge_step_fen: number;
  }) => Promise<BillingSettings>;
  testProvider: (provider: ProviderName) => Promise<ProviderTestResult>;
  /**
   * 付费探针（可选注入）。只有管理端控制面注入：它可能真实扣费，服务端按
   * 「敏感写」受理（`confirm` + 非空 `reason` + 幂等键 + 审计），所以这里多一个
   * `reason` 参数。工作台面后端不注入它——付费探针是运维动作，不该出现在客户
   * 构建制品里（与 `/api/control/` 零命中的同一道边界）。
   */
  testPaidProvider?: (
    provider: ProviderName,
    reason: string,
  ) => Promise<ProviderTestResult>;
};

const workspaceBackend: SettingsBackend = {
  load: getSettings,
  saveProvider: updateProviderSettings,
  saveRuntime: updateRuntimeSettings,
  saveBilling: updateBillingSettings,
  testProvider: testProviderConnection,
};

export type SettingsPanelProps = {
  section?: "all" | "providers" | "runtime";
  videoAccounts?: ReactNode;
} & (
  | { source?: "workspace"; readOnly?: boolean }
  | {
      source: "control";
      controlBackend: SettingsBackend;
      readOnly?: boolean;
    }
);

function visibleErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : fallback;
}

type ProviderField = {
  name: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
};

type ProviderFormSpec = {
  title: string;
  note?: string;
  fields: ProviderField[];
  /**
   * 备用通道分组（目前只有爆款数据源用）。
   *
   * 备用入口填了才启用：服务端在主入口失效（超时 / 限流 / 上游异常）时自动切过去
   * 重取。它对客户是不可见的计费细节——切换只发生在平台侧，客户侧价格与扣费口径
   * 不变，所以这里必须把「已启用 / 怎么启用」写清楚，而不是留两个裸输入框。
   */
  backupChannel?: {
    heading: string;
    fields: ProviderField[];
    enabledNotice: string;
    hint: string;
  };
};

// COS 区域固定为上海，界面不再显示 Region 输入框。
const COS_REGION = "ap-shanghai";

const PROVIDER_FORMS: Record<ProviderName, ProviderFormSpec> = {
  metaso: {
    title: "视频生成",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  apilio: {
    title: "模型服务",
    note: "视频分析模型留空则用服务端默认值；主模型限流或明确下线时自动切换一次。备选留空用内置模型，最多配置一个；填写 disabled 可关闭切换",
    fields: [
      { name: "api_key", label: "图像模型 API Key", secret: true },
      {
        name: "analysis_api_key",
        label: "视频分析 API Key（可选）",
        secret: true,
      },
      {
        name: "analysis_model",
        label: "视频分析模型（可选）",
        placeholder: "gemini-3.8-flash",
      },
      {
        name: "analysis_model_fallbacks",
        label: "备选视频分析模型（可选，最多一个）",
        placeholder: "gemini-3.7-flash",
      },
    ],
  },
  cos: {
    title: "腾讯云存储",
    note: "区域固定为上海 · 测试连接会在七个业务目录分别创建、校验并删除临时对象，包含爆款封面和视频",
    fields: [
      { name: "access_key_id", label: "SecretId", secret: true },
      { name: "secret_access_key", label: "SecretKey", secret: true },
      { name: "bucket", label: "Bucket" },
    ],
  },
  deepseek: {
    title: "文本 AI · DeepSeek",
    note: "文本 AI 统一使用 DeepSeek；人物 IP 和自定义要求共用此配置",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  hifly: {
    title: "数字人口播",
    note: "Hifly · 只读检查账户余额，不会创建收费任务",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  tikhub: {
    title: "爆款视频数据源",
    note: "抖音 / 视频号最近 7 天爆款参考库。主入口失效（超时、限流、上游异常）时自动切到备用入口重取，同一份数据不会重复计成本。",
    fields: [{ name: "api_key", label: "主入口密钥", secret: true }],
    backupChannel: {
      heading: "备用通道（可选）",
      enabledNotice:
        "备用通道已启用：关键词采集、视频号分页、视频号详情 / 统计补采三个计费节点共用这条通道；切换只发生在平台侧，客户端不重复扣费——一次请求只在成功的那条通道上记一次平台成本。",
      hint: "备用密钥必须与备用入口一起配置：只填密钥不填入口，保存会被拒绝。留空「备用密钥」表示与主入口共用同一把密钥。",
      fields: [
        {
          name: "backup_base_url",
          label: "备用入口",
          placeholder: "https://…（留空则不启用备用通道）",
        },
        { name: "backup_api_key", label: "备用密钥", secret: true },
      ],
    },
  },
  dashscope: {
    title: "语音转写",
    note: "上传视频提取文案 · 只需 API Key",
    fields: [{ name: "api_key", label: "API Key", secret: true }],
  },
  douyidou: {
    title: "链接解析",
    note: "抖音 / 快手 / 小红书链接去水印与文案提取",
    fields: [
      { name: "app_id", label: "App ID" },
      { name: "app_secret", label: "App Secret", secret: true },
    ],
  },
  ses: {
    title: "邮件推送",
    note: "客户绑定邮箱与找回密码的验证码邮件。只按审核过的模板发送；验证码模板必填，通知模板留空时重置成功不发通知。测试连接仅核对凭据与模板，不发信。",
    fields: [
      { name: "secret_id", label: "SecretId", secret: true },
      { name: "secret_key", label: "SecretKey", secret: true },
      {
        name: "from_address",
        label: "发信地址",
        placeholder: "名称 <noreply@example.com>",
      },
      { name: "code_template_id", label: "验证码模板 ID" },
      { name: "notice_template_id", label: "通知模板 ID（可选）" },
      {
        name: "region",
        label: "地域（可选）",
        placeholder: "ap-guangzhou",
      },
    ],
  },
};

const PROVIDER_ORDER: ProviderName[] = [
  "metaso",
  "apilio",
  "cos",
  "deepseek",
  "hifly",
  "tikhub",
  "dashscope",
  "douyidou",
  "ses",
];

export function SettingsPanel(props: SettingsPanelProps) {
  const source = props.source ?? "workspace";
  const section = props.section ?? "all";
  const readOnly = props.readOnly ?? false;
  const backend =
    props.source === "control" ? props.controlBackend : workspaceBackend;
  const [settings, setSettings] = useState<SettingsSnapshot | null>(null);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let isMounted = true;
    backend
      .load()
      .then((snapshot) => {
        if (isMounted) {
          setSettings(snapshot);
          setLoadError("");
        }
      })
      .catch((error: unknown) => {
        if (isMounted) {
          setLoadError(
            visibleErrorMessage(
              error,
              "无法读取设置。请确认本地服务已启动且当前身份具有管理员权限。",
            ),
          );
        }
      });

    return () => {
      isMounted = false;
    };
  }, [backend]);

  async function saveProvider(
    provider: ProviderName,
    config: Record<string, string>,
  ) {
    const finalConfig =
      provider === "cos" ? { ...config, region: COS_REGION } : config;
    const updated = await backend.saveProvider(provider, finalConfig);
    setSettings((current) =>
      current
        ? {
            ...current,
            providers: { ...current.providers, [provider]: updated },
          }
        : current,
    );
  }

  async function saveRuntime(runtime: RuntimeSettings) {
    const updated = await backend.saveRuntime(runtime);
    setSettings((current) =>
      current ? { ...current, runtime: updated } : current,
    );
  }

  async function revealSavedSecret(provider: ProviderName, field: string) {
    if (source === "control") {
      throw new Error("控制台不支持显示已保存密钥");
    }
    return revealProviderSecret(provider, field);
  }

  async function saveBilling(billing: BillingSettings) {
    const updated = await backend.saveBilling({
      internal_base_unit_price_fen: billing.internal_base_unit_price_fen,
      oral_unit_price_fen: billing.oral_unit_price_fen,
      min_recharge_fen: billing.min_recharge_fen,
      recharge_step_fen: billing.recharge_step_fen,
    });
    setSettings((current) =>
      current ? { ...current, billing: updated } : current,
    );
  }

  if (loadError) {
    return (
      <section className="settings-error" role="alert">
        {loadError}
      </section>
    );
  }

  if (!settings) {
    return <p className="status-note">正在读取服务设置</p>;
  }

  return (
    <section className="settings-page" aria-label="服务设置">
      {section !== "runtime" && (
        <div className="provider-grid">
          {props.videoAccounts}
          {PROVIDER_ORDER.map((provider) => {
            if (provider === "metaso" && props.videoAccounts) return null;
            // 服务端快照可能落后于前端枚举（灰度/旧版本），缺失的 provider
            // 直接跳过，不让整个设置页白屏。
            const providerSettings = settings.providers[provider];
            if (!providerSettings) return null;
            return (
              <ProviderForm
                key={provider}
                provider={provider}
                readOnly={readOnly}
                settings={providerSettings}
                onSave={saveProvider}
                onReveal={
                  source === "workspace" ? revealSavedSecret : undefined
                }
                onTest={backend.testProvider}
                onPaidTest={backend.testPaidProvider}
              />
            );
          })}
        </div>
      )}

      {section !== "providers" && (
        <RuntimeForm
          accountConcurrency={source === "control"}
          readOnly={readOnly}
          runtime={settings.runtime}
          onSave={saveRuntime}
        />
      )}
      {source === "workspace" && section === "all" && (
        <OralPriceForm
          readOnly={readOnly}
          billing={settings.billing}
          onSave={saveBilling}
        />
      )}
    </section>
  );
}

function OralPriceForm({
  billing,
  readOnly,
  onSave,
}: {
  billing: BillingSettings;
  readOnly: boolean;
  onSave: (billing: BillingSettings) => Promise<void>;
}) {
  const [priceYuan, setPriceYuan] = useState(
    (billing.oral_unit_price_fen / 100).toString(),
  );
  const [status, setStatus] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) return;
    const numericPrice = Number(priceYuan);
    const oralUnitPriceFen = Math.round(numericPrice * 100);
    if (!Number.isFinite(numericPrice) || oralUnitPriceFen < 1) {
      setStatus("口播单价必须大于 0 元");
      return;
    }
    setIsSaving(true);
    setStatus("");
    try {
      await onSave({ ...billing, oral_unit_price_fen: oralUnitPriceFen });
      setStatus("口播价格已保存");
    } catch {
      setStatus("口播价格保存失败");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <form className="runtime-form" onSubmit={submit}>
      <h3>数字人口播价格</h3>
      <div className="runtime-fields">
        <label>
          数字人口播单价（元/条）
          <input
            disabled={readOnly || isSaving}
            type="number"
            min="0.01"
            step="0.01"
            value={priceYuan}
            onChange={(event) => setPriceYuan(event.target.value)}
          />
        </label>
      </div>
      <p className="storage-provider-hint">
        任务创建时冻结价格快照，后续改价不影响已创建任务。
      </p>
      <div className="form-actions">
        <button disabled={readOnly || isSaving} type="submit">
          {isSaving ? "正在保存" : "保存口播价格"}
        </button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

function ProviderForm({
  provider,
  readOnly,
  settings,
  onSave,
  onReveal,
  onTest,
  onPaidTest,
}: {
  provider: ProviderName;
  readOnly: boolean;
  settings: ProviderSettings;
  onSave: (
    provider: ProviderName,
    config: Record<string, string>,
  ) => Promise<void>;
  onReveal?: (provider: ProviderName, field: string) => Promise<string>;
  onTest: (provider: ProviderName) => Promise<ProviderTestResult>;
  /**
   * 付费探针。只有管理端注入时才会渲染入口——工作台面（客户构建）没有它，
   * 免费连接测试与付费探针因此只在管理端「真正并列」。
   */
  onPaidTest?: (
    provider: ProviderName,
    reason: string,
  ) => Promise<ProviderTestResult>;
}) {
  const form = PROVIDER_FORMS[provider];
  const backupChannel = form.backupChannel;
  const backupChannelFields = backupChannel?.fields ?? [];
  // 主字段与备用通道字段共用一份表单状态：初始化要看得到备用入口的已存值，
  // 提交后也要清掉备用密钥，否则保存完输入框里还留着上一次的密钥。
  const formFields = useMemo(
    () => [...form.fields, ...(form.backupChannel?.fields ?? [])],
    [form],
  );
  const [values, setValues] = useState<Record<string, string>>(() =>
    initialValues(formFields, settings.config),
  );
  const [visibleFields, setVisibleFields] = useState<Record<string, boolean>>(
    {},
  );
  const [revealedFields, setRevealedFields] = useState<Record<string, boolean>>(
    {},
  );
  const [revealingFields, setRevealingFields] = useState<
    Record<string, boolean>
  >({});
  const [status, setStatus] = useState("");
  const [statusTone, setStatusTone] = useState<"ok" | "error">("ok");
  const [backupError, setBackupError] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [paidProbeOpen, setPaidProbeOpen] = useState(false);
  const [paidProbeReason, setPaidProbeReason] = useState("");
  const [isPaidProbing, setIsPaidProbing] = useState(false);
  const previousConfigRef = useRef(settings.config);

  useEffect(() => {
    if (previousConfigRef.current === settings.config) {
      return;
    }
    previousConfigRef.current = settings.config;
    setValues(initialValues(formFields, settings.config));
  }, [formFields, settings.config]);

  async function toggleSecretVisibility(name: string) {
    if (visibleFields[name]) {
      setVisibleFields((current) => ({ ...current, [name]: false }));
      if (revealedFields[name]) {
        setValues((current) => ({ ...current, [name]: "" }));
        setRevealedFields((current) => ({ ...current, [name]: false }));
      }
      return;
    }

    if (values[name] || !settings.configured) {
      setVisibleFields((current) => ({ ...current, [name]: true }));
      return;
    }

    if (!onReveal || revealingFields[name]) {
      return;
    }

    setRevealingFields((current) => ({ ...current, [name]: true }));
    setStatus("");
    try {
      const value = await onReveal(provider, name);
      setValues((current) => ({ ...current, [name]: value }));
      setRevealedFields((current) => ({ ...current, [name]: true }));
      setVisibleFields((current) => ({ ...current, [name]: true }));
    } catch (error) {
      setStatus(visibleErrorMessage(error, "读取已保存密钥失败。"));
      setStatusTone("error");
    } finally {
      setRevealingFields((current) => ({ ...current, [name]: false }));
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSaving) {
      return;
    }
    setIsSaving(true);
    setStatus("");
    setBackupError("");
    try {
      await onSave(provider, values);
      setValues((current) => clearSecretFields(current, formFields));
      setVisibleFields({});
      setRevealedFields({});
      setStatus("已保存");
      setStatusTone("ok");
    } catch (cause) {
      const message = saveErrorMessage(cause);
      // 备用通道的错配只有服务端判得准（入口格式、密钥与入口是否配对），
      // 这类消息同时落在分组里，操作者不必回头去顶部状态行找原因。
      if (message.includes("备用")) {
        setBackupError(message);
      }
      setStatus(message);
      setStatusTone("error");
    } finally {
      setIsSaving(false);
    }
  }

  async function handleTest() {
    if (isTesting) {
      return;
    }
    setIsTesting(true);
    setStatus("");
    try {
      const result = await onTest(provider);
      setStatus(testResultLabel(result));
      setStatusTone(testResultSucceeded(result) ? "ok" : "error");
    } catch (error) {
      setStatus(
        visibleErrorMessage(error, "测试失败，请检查网络与管理员权限后重试。"),
      );
      setStatusTone("error");
    } finally {
      setIsTesting(false);
    }
  }

  function openPaidProbe() {
    // 每次打开都是一次新的确认：不留下上一次的原因或结果。
    setPaidProbeReason("");
    setStatus("");
    setPaidProbeOpen(true);
  }

  async function runPaidProbe() {
    if (!onPaidTest || isPaidProbing) {
      return;
    }
    const reason = paidProbeReason.trim();
    if (!reason) {
      setStatus("请填写付费探针的操作原因（会写入审计）");
      setStatusTone("error");
      return;
    }
    setIsPaidProbing(true);
    setStatus("");
    try {
      const result = await onPaidTest(provider, reason);
      setStatus(paidProbeResultLabel(result));
      setStatusTone(testResultSucceeded(result) ? "ok" : "error");
      setPaidProbeOpen(false);
      setPaidProbeReason("");
    } catch (error) {
      setStatus(paidProbeErrorMessage(error));
      setStatusTone("error");
    } finally {
      setIsPaidProbing(false);
    }
  }

  function renderField(field: ProviderField) {
    const isVisible = Boolean(visibleFields[field.name]);
    const isRevealing = Boolean(revealingFields[field.name]);
    return (
      <label key={field.name}>
        {field.label}
        <span className={field.secret ? "secret-field" : undefined}>
          <input
            disabled={readOnly || isRevealing || isSaving}
            autoComplete={field.secret ? "new-password" : "off"}
            type={field.secret && !isVisible ? "password" : "text"}
            value={values[field.name] ?? ""}
            placeholder={
              field.secret && settings.configured
                ? "已保存，留空不修改"
                : field.placeholder
            }
            onChange={(event) => {
              setValues((current) => ({
                ...current,
                [field.name]: event.target.value,
              }));
              setRevealedFields((current) => ({
                ...current,
                [field.name]: false,
              }));
            }}
          />
          {field.secret ? (
            <button
              disabled={readOnly || isRevealing}
              type="button"
              className="secret-toggle"
              aria-label={
                isRevealing
                  ? `正在读取${field.label}`
                  : isVisible
                    ? `隐藏${field.label}`
                    : `显示${field.label}`
              }
              aria-pressed={isVisible}
              onClick={() => void toggleSecretVisibility(field.name)}
            >
              <svg
                aria-hidden="true"
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
                <circle cx="12" cy="12" r="3" />
                {isVisible ? <path d="m3 3 18 18" /> : null}
              </svg>
            </button>
          ) : null}
        </span>
      </label>
    );
  }

  return (
    <form
      className="provider-card"
      data-provider={provider}
      onSubmit={submit}
      autoComplete="off"
    >
      <div className="provider-card__heading">
        <h3>{form.title}</h3>
        {form.note ? <p>{form.note}</p> : null}
      </div>
      <span
        className={
          settings.configured
            ? "config-state config-state--ready"
            : "config-state"
        }
      >
        {settings.configured ? "已配置" : "未配置"}
      </span>
      <div className="field-stack">
        {form.fields.map(renderField)}
        {backupChannel ? (
          <div
            className="provider-backup"
            data-invalid={backupError ? "true" : undefined}
          >
            <p className="provider-backup__heading">{backupChannel.heading}</p>
            {backupChannelFields.some(
              (field) => (settings.config[field.name] ?? "").trim() !== "",
            ) ? (
              <p className="provider-backup__notice">
                {/* 图标单独成节：读屏不必念出装饰符号，正文也保持可独立匹配。 */}
                <span aria-hidden="true">✓</span> {backupChannel.enabledNotice}
              </p>
            ) : null}
            {backupError ? (
              // 不重复声明 role="alert"：顶部状态行已经播报过一次，读屏不该听两遍。
              <p className="provider-backup__error">
                <span aria-hidden="true">✕</span> {backupError}
              </p>
            ) : null}
            {backupChannelFields.map(renderField)}
            <p className="provider-backup__hint">{backupChannel.hint}</p>
          </div>
        ) : null}
      </div>
      <div className="form-actions">
        <button type="submit" disabled={readOnly || isSaving || isTesting}>
          {isSaving ? "正在保存" : "保存"}
        </button>
        <button
          type="button"
          className="secondary-button"
          onClick={handleTest}
          disabled={readOnly || isSaving || isTesting}
        >
          {isTesting
            ? provider === "hifly"
              ? "正在检查"
              : "正在测试"
            : provider === "hifly"
              ? "只读检查"
              : "测试连接"}
        </button>
        {onPaidTest ? (
          <button
            type="button"
            className="secondary-button"
            onClick={openPaidProbe}
            disabled={readOnly || isSaving || isTesting || isPaidProbing}
          >
            付费探针
          </button>
        ) : null}
        {status ? (
          <span
            role={statusTone === "error" ? "alert" : "status"}
            className={
              statusTone === "error" ? "form-status--error" : undefined
            }
          >
            {status}
          </span>
        ) : null}
      </div>
      {onPaidTest && paidProbeOpen ? (
        <div className="paid-probe-confirm field-stack">
          <p className="paid-probe-confirm__hint">
            付费探针：验证该服务的账号能否真正跑通一次计费调用。它可能产生供应商
            侧费用，因此服务端按敏感写受理，需要操作原因并写入审计。
          </p>
          <p className="paid-probe-confirm__status">
            {provider === "hifly"
              ? "现状：该服务已接入真实客户端——执行会真实提交一次最小计费调用（短文本语音合成），可能产生供应商侧费用，执行后请核对账单。"
              : "现状：真实供应商客户端尚未接入，执行后服务端只会返回 501「未实现」，不会创建供应商任务，也不会产生任何费用。"}
          </p>
          <label>
            操作原因（必填，写入审计）
            <input
              value={paidProbeReason}
              onChange={(event) => setPaidProbeReason(event.target.value)}
            />
          </label>
          <div className="form-actions">
            <button
              type="button"
              onClick={() => void runPaidProbe()}
              disabled={isPaidProbing}
            >
              {isPaidProbing ? "正在执行" : "确认执行付费探针"}
            </button>
            <button
              type="button"
              className="secondary-button"
              onClick={() => setPaidProbeOpen(false)}
              disabled={isPaidProbing}
            >
              取消
            </button>
          </div>
        </div>
      ) : null}
    </form>
  );
}

function RuntimeForm({
  accountConcurrency = false,
  runtime,
  readOnly,
  onSave,
}: {
  accountConcurrency?: boolean;
  runtime: RuntimeSettings;
  readOnly: boolean;
  onSave: (runtime: RuntimeSettings) => Promise<void>;
}) {
  const [values, setValues] = useState(runtime);
  const [status, setStatus] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const previousRuntimeRef = useRef(runtime);

  useEffect(() => {
    if (previousRuntimeRef.current === runtime) {
      return;
    }
    previousRuntimeRef.current = runtime;
    setValues(runtime);
  }, [runtime]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await save(values);
  }

  async function save(next: RuntimeSettings) {
    if (isSaving) {
      return;
    }
    setStatus("");
    const limitsValid =
      Number.isInteger(next.max_generation_count_per_batch) &&
      next.max_generation_count_per_batch >= 1 &&
      Number.isInteger(next.max_concurrent_h3_tasks) &&
      next.max_concurrent_h3_tasks >= 1;
    if (!limitsValid) {
      setStatus("数量上限与并发数必须为 ≥1 的整数");
      return;
    }
    setIsSaving(true);
    try {
      await onSave(next);
      setStatus("已保存");
    } catch {
      setStatus("保存失败");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <form className="runtime-form" onSubmit={submit}>
      <h3>运行设置</h3>
      <div className="runtime-fields">
        <label>
          单次生成数量上限
          <input
            disabled={readOnly || isSaving}
            type="number"
            min="1"
            value={values.max_generation_count_per_batch}
            onChange={(event) =>
              setValues((current) => ({
                ...current,
                max_generation_count_per_batch: Number(event.target.value),
              }))
            }
          />
        </label>
        {accountConcurrency ? (
          <p>视频生成并发在“API 服务”中按账号设置。</p>
        ) : (
          <label>
            视频生成并发数
            <input
              disabled={readOnly || isSaving}
              type="number"
              min="1"
              value={values.max_concurrent_h3_tasks}
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  max_concurrent_h3_tasks: Number(event.target.value),
                }))
              }
            />
          </label>
        )}
      </div>
      <p className="storage-provider-hint">
        人物图片、参考视频与首帧保存到腾讯云存储（需在桶 CORS 放行
        PUT/GET/HEAD，否则上传失败）；生成的成片可在任务结果中保存到素材库，或下载到本机。
      </p>
      {runtime.active_storage_provider === "local" ? (
        <div>
          <p>
            当前仍使用本地存储，云端分析无法读取本地视频。请先通过腾讯云连接测试，再启用云存储并重新上传素材。
          </p>
          <button
            type="button"
            disabled={readOnly || isSaving}
            onClick={() =>
              void save({ ...runtime, active_storage_provider: "cos" })
            }
          >
            启用腾讯云存储
          </button>
        </div>
      ) : null}
      <div className="form-actions">
        <button disabled={readOnly || isSaving} type="submit">
          {isSaving ? "正在保存" : "保存"}
        </button>
        {status ? <span role="status">{status}</span> : null}
      </div>
    </form>
  );
}

/**
 * 保存失败的文案。
 *
 * 服务端的校验消息（例如备用通道「密钥没有入口」「入口不是 http(s)」）是操作者唯一
 * 能据以自救的线索，不能吞掉换成「保存失败」；但也只在这类明确的校验失败上原样回显——
 * 网络中断、网关错误页会带英文原文，套到界面上反而更难懂。
 */
function saveErrorMessage(error: unknown): string {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? (error as { code?: unknown }).code
      : undefined;
  if (
    (code === "INVALID_SETTINGS" || code === "INVALID_SERVICE_SETTINGS") &&
    error instanceof Error &&
    error.message.trim()
  ) {
    return error.message;
  }
  return "保存失败，请检查必填项与管理员权限。";
}

function testResultLabel(result: ProviderTestResult) {
  switch (result.status) {
    case "ok":
      if (result.provider === "hifly") {
        if (hasValidHiflyCredit(result)) {
          return `只读账户检查通过，余额 ${result.account_credit} 积分；未创建收费任务`;
        }
        return "只读账户检查响应异常，请稍后重试。";
      }
      return "连接测试通过";
    case "configured_only":
      return "参数已保存；测试不会发起外部调用";
    default:
      return "尚未保存该服务的必要参数";
  }
}

function paidProbeResultLabel(result: ProviderTestResult) {
  switch (result.status) {
    case "ok":
      return "付费探针通过：供应商账号可完成一次计费调用";
    case "configured_only":
      return "参数已保存；本次未发起计费调用";
    default:
      return "尚未保存该服务的必要参数，本次未发起计费调用";
  }
}

/**
 * 付费探针的失败文案。
 *
 * `PROVIDER_TEST_NOT_IMPLEMENTED` 只会在尚未接入真实客户端的服务上出现：
 * 服务端 501，且没有创建任何供应商任务、没有产生任何费用。因此这里如实说明
 * "未执行、未计费"，不得改写成"测试失败请重试"或任何暗示已经花了钱的措辞——
 * 操作者据此才会（或不会）去核对账单。
 *
 * 错误码的结构化读取刻意不 import 管理端错误类：本组件被客户入口静态复用，
 * 静态引用 `api.admin` 会把管理域代码拖进客户构建制品（CW-019 / entryContract）。
 */
function paidProbeErrorMessage(error: unknown) {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? (error as { code?: unknown }).code
      : undefined;
  if (code === "PROVIDER_TEST_NOT_IMPLEMENTED") {
    return "付费探针未执行：该服务尚未接入真实供应商客户端（服务端 501 未实现），未产生任何费用。";
  }
  return visibleErrorMessage(error, "付费探针执行失败，请稍后重试。");
}

function hasValidHiflyCredit(result: ProviderTestResult) {
  return (
    typeof result.account_credit === "number" &&
    Number.isSafeInteger(result.account_credit) &&
    result.account_credit >= 0
  );
}

function testResultSucceeded(result: ProviderTestResult) {
  return (
    result.status === "configured_only" ||
    (result.status === "ok" &&
      (result.provider !== "hifly" || hasValidHiflyCredit(result)))
  );
}

function initialValues(
  fields: ProviderField[],
  config: Record<string, string>,
) {
  return Object.fromEntries(
    fields.map((field) => [
      field.name,
      field.secret ? "" : (config[field.name] ?? ""),
    ]),
  );
}

function clearSecretFields(
  values: Record<string, string>,
  fields: ProviderField[],
) {
  const next = { ...values };
  for (const field of fields) {
    if (field.secret) {
      next[field.name] = "";
    }
  }
  return next;
}
