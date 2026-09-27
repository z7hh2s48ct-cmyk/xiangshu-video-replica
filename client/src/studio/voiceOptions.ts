import type { OralVoiceLanguage, OralVoiceSettings } from "../api";

export const DEFAULT_VOICE_LANGUAGE: OralVoiceLanguage = "zh";

export const VOICE_LANGUAGE_OPTIONS: ReadonlyArray<{
  value: OralVoiceLanguage;
  label: string;
}> = [
  { value: "zh", label: "普通话" },
  { value: "zh_cantonese", label: "粤语" },
  { value: "zh_sichuanese", label: "四川话" },
  { value: "zh_shanghainese", label: "上海话" },
  { value: "zh_tianjinese", label: "天津话" },
  { value: "zh_zhengzhounese", label: "郑州话" },
  { value: "zh_wuhanese", label: "武汉话" },
];

export function voiceLanguageLabel(language: OralVoiceLanguage): string {
  return (
    VOICE_LANGUAGE_OPTIONS.find((option) => option.value === language)?.label ??
    "普通话"
  );
}

export const DEFAULT_VOICE_SETTINGS: OralVoiceSettings = {
  speechRate: 1,
  volume: 1,
  pitch: 1,
};

export type VoiceSettingPreset = { label: string; value: number };

/**
 * 取值范围与服务端校验一致；步进 0.1 对应库里的一位小数。
 * 每项先给三档预设让用户一键选择，滑块只作微调——多数人不需要理解数值。
 */
export const VOICE_SETTING_FIELDS: ReadonlyArray<{
  key: keyof OralVoiceSettings;
  label: string;
  min: number;
  max: number;
  description: string;
  presets: ReadonlyArray<VoiceSettingPreset>;
}> = [
  {
    key: "speechRate",
    label: "语速",
    min: 0.5,
    max: 2,
    description: "说话的快慢，调得过快可能听不清。",
    presets: [
      { label: "较慢", value: 0.8 },
      { label: "标准", value: 1 },
      { label: "较快", value: 1.2 },
    ],
  },
  {
    key: "volume",
    label: "音量",
    min: 0.1,
    max: 2,
    description: "声音的大小，调得过大可能失真。",
    presets: [
      { label: "较轻", value: 0.8 },
      { label: "标准", value: 1 },
      { label: "较响", value: 1.3 },
    ],
  },
  {
    key: "pitch",
    label: "音调",
    min: 0.1,
    max: 2,
    description: "声音的高低，偏离太多会不像本人。",
    presets: [
      { label: "偏低", value: 0.9 },
      { label: "标准", value: 1 },
      { label: "偏高", value: 1.1 },
    ],
  },
];

/** 命中预设时返回档位名（如「较快」），否则返回一位小数的数值。 */
export function describeVoiceSetting(
  key: keyof OralVoiceSettings,
  value: number,
): string {
  const field = VOICE_SETTING_FIELDS.find((item) => item.key === key);
  const preset = field?.presets.find(
    (item) => roundVoiceSetting(item.value) === roundVoiceSetting(value),
  );
  return preset ? preset.label : value.toFixed(1);
}

export const VOICE_SETTING_STEP = 0.1;

/** range 输入会产生 1.2000000000000002 这类浮点值，统一收敛到一位小数。 */
export function roundVoiceSetting(value: number): number {
  return Math.round(value * 10) / 10;
}

export function formatVoiceSettings(settings: OralVoiceSettings): string {
  return VOICE_SETTING_FIELDS.map(
    (field) =>
      `${field.label} ${describeVoiceSetting(field.key, settings[field.key])}`,
  ).join(" · ");
}
