// Runs only in an isolated official creator window. Forward identity fields only.
(() => {
  if (window.top !== window) return;
  const allowed = {
    "https://creator.douyin.com": "/web/api/media/user/info/",
    "https://channels.weixin.qq.com": "/auth/auth_data",
    "https://creator.xiaohongshu.com": "/api/galaxy/user/info",
  };
  const endpoint = allowed[location.origin];
  if (!endpoint) return;
  const matches = (url) => {
    try { const parsed = new URL(url, location.href); return parsed.origin === location.origin && parsed.pathname.endsWith(endpoint); } catch { return false; }
  };
  // Avatars are public CDN links. Only bounded https URLs leave the official frame,
  // so a hostile response cannot smuggle data through a data:/javascript: value.
  const validAvatar = (value) =>
    typeof value === "string" && value.trim().length <= 1024 && /^https:\/\/\S+$/.test(value.trim());
  const capture = (url, body) => {
    try {
      const parsed = new URL(url, location.href);
      if (parsed.origin !== location.origin || !parsed.pathname.endsWith(endpoint)) return;
      let uid, name, avatar;
      if (location.hostname === "creator.douyin.com" && Number(body.status_code) === 0) {
        uid = body.user?.uid ?? body.user?.user_id;
        name = body.user?.nickname;
        avatar = body.user?.avatar_larger?.url_list?.[0] ?? body.user?.avatar_thumb?.url_list?.[0] ?? body.user?.avatar_url;
      } else if (location.hostname === "channels.weixin.qq.com" && Number(body.errCode ?? body.errcode) === 0) {
        const user = body.data?.finderUser ?? body.data?.userAttr ?? body.data?.finderList?.[0];
        uid = user?.finderUsername;
        name = user?.nickname ?? user?.nickName;
        avatar = user?.headImgUrl ?? user?.headImage;
      } else if (location.hostname === "creator.xiaohongshu.com" && (body.success === true || (body.success === undefined && body.code === 0))) {
        uid = body.data?.userId;
        name = body.data?.userName;
      }
      if ((typeof uid === "string" || (typeof uid === "number" && Number.isSafeInteger(uid))) && typeof name === "string" && String(uid).trim() && String(uid).length <= 256 && name.trim()) {
        const identity = {platform_user_id: String(uid).trim(), username: name.trim().slice(0, 256)};
        if (validAvatar(avatar)) identity.avatar_url = avatar.trim();
        window.__xiangshuPublishIdentity = identity;
      }
    } catch { /* Non-JSON and unrelated responses are ignored. */ }
  };
  const fetchOriginal = window.fetch;
  window.fetch = async function (...args) {
    const response = await fetchOriginal.apply(this, args);
    const url = response.url || (typeof args[0] === "string" ? args[0] : args[0]?.url);
    if (matches(url)) void response.clone().json().then(body => capture(url, body)).catch(() => {});
    return response;
  };
  const openOriginal = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (...args) {
    this.addEventListener("load", () => {
      try { if (matches(this.responseURL)) capture(this.responseURL, this.responseType === "json" ? this.response : JSON.parse(this.responseText)); } catch { /* Unrelated response. */ }
    }, {once: true});
    return openOriginal.apply(this, args);
  };

  // Douyin issues its identity request across the post-scan navigation, so passive
  // interception alone keeps missing it and the account is never recognised. Ask the
  // page's own session for the same endpoint until the identity is known; the wrapped
  // fetch above captures the response, so there is one parser and no extra surface.
  // The other platforms call their endpoint reliably on every visit and need no probe.
  if (location.hostname === "creator.douyin.com" && typeof window.setInterval === "function") {
    let attempts = 0;
    const timer = window.setInterval(() => {
      if (window.__xiangshuPublishIdentity || ++attempts > 100) {
        window.clearInterval?.(timer);
        return;
      }
      try {
        void Promise.resolve(window.fetch("/web/api/media/user/info/?aid=1128", {credentials: "include"})).catch(() => {});
      } catch { /* A blocked request must not stop the next attempt. */ }
    }, 3000);
  }
})();
