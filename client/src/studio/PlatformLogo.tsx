import { useState } from "react";

const platforms = {
  douyin: { name: "抖音", file: "douyin" },
  wechat_channels: { name: "视频号", file: "wechat_channels" },
  xiaohongshu: { name: "小红书", file: "xiaohongshu" },
};

export type PublishPlatform = keyof typeof platforms;
export const publishPlatformNames = Object.fromEntries(
  Object.entries(platforms).map(([key, value]) => [key, value.name]),
) as Record<PublishPlatform, string>;

export function PlatformLogo({
  platform,
  size = 24,
}: {
  platform: string;
  size?: number;
}) {
  const item =
    platforms[platform as PublishPlatform] ??
    Object.values(platforms).find((value) => value.name === platform);
  if (!item) return null;
  return (
    <img
      src={`/platforms/${item.file}.ico`}
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      style={{ objectFit: "contain", verticalAlign: "middle", flexShrink: 0 }}
    />
  );
}

/** The account's picture, falling back to the platform mark when it cannot load. */
export function AccountAvatar({
  account,
  size = 32,
}: {
  account: { platform: string; username: string; avatar_url?: string | null };
  size?: number;
}) {
  const [failed, setFailed] = useState(false);
  if (!account.avatar_url || failed)
    return <PlatformLogo platform={account.platform} size={size} />;
  return (
    <img
      className="publish-account-avatar"
      src={account.avatar_url}
      alt={`${account.username} 的头像`}
      width={size}
      height={size}
      // Platform CDNs expire links and check referrers; the mark stands in for a
      // picture that will not load, so a row never renders as a broken image.
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}
