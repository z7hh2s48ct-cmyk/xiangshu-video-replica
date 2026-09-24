/* Platform selectors adapted from dreammis/social-auto-upload (MIT).
 * Full license shipped in frontend assets: /licenses/social-auto-upload-MIT.txt
 * Only QR images/status leave an official frame; credentials stay in WebView2.
 */
(() => {
  const channels = "https://channels.weixin.qq.com";
  const wechat = "https://open.weixin.qq.com";
  const origin = location.origin;
  const child = window.top !== window;
  const official = ["https://creator.douyin.com", channels, "https://creator.xiaohongshu.com"];
  const qrFrame = (url) =>
    (url.origin === wechat && url.pathname === "/connect/qrconnect") ||
    (url.origin === channels && url.pathname.includes("login-for-iframe"));
  if (child ? !qrFrame(location) : !official.includes(origin)) return;

  const validImage = (value) => typeof value === "string" && value.length <= 600000 &&
    /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/.test(value);
  const visible = (element) => {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
  };
  const first = (selectors, root = document) => {
    for (const selector of selectors) {
      const element = [...root.querySelectorAll(selector)].find(visible);
      if (element) return element;
    }
    return null;
  };
  const report = (phase, image = null) => {
    const value = { phase, image: validImage(image) ? image : null, observed_at: Date.now() };
    if (child) window.top.postMessage({ type: "xiangshu-publish-qr", ...value }, channels);
    else window.__xiangshuPublishLogin = value;
  };
  let bridge = null;
  if (!child && origin === channels) {
    window.addEventListener("message", (event) => {
      if (![channels, wechat].includes(event.origin) || !event.source) return;
      const frame = [...document.querySelectorAll("iframe")].find((element) => {
        try {
          const url = new URL(element.getAttribute("src"), location.href);
          return element.contentWindow === event.source && url.origin === event.origin && qrFrame(url);
        } catch { return false; }
      });
      if (!frame || event.data?.type !== "xiangshu-publish-qr") return;
      const { phase, image } = event.data;
      if (!["loading", "qr_ready", "expired", "confirming", "action_required"].includes(phase)) return;
      if (phase === "qr_ready" && !validImage(image)) return;
      bridge = { frame, phase, image: phase === "qr_ready" ? image : null, at: Date.now() };
    });
  }

  let switched = false;
  let cachedSource = "";
  let cachedImage = null;
  function readImage(image) {
    const source = image.getAttribute("src") || "";
    if (validImage(source)) return source;
    // Canvas reads the image already rendered by the official browser session.
    // No image URL, Cookie, token, or arbitrary resource is fetched by our app.
    if (source === cachedSource && cachedImage) return cachedImage;
    if (!image.complete || !image.naturalWidth || image.naturalWidth > 2048 || image.naturalHeight > 2048) return null;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      canvas.getContext("2d").drawImage(image, 0, 0);
      const data = canvas.toDataURL("image/png");
      if (!validImage(data)) return null;
      cachedSource = source;
      cachedImage = data;
      return data;
    } catch { return null; } // Cross-origin image: keep the official window usable.
  }

  function scan() {
    if (!document.body) return;
    // Look only at visible leaf text, so hidden expired-QR templates do not win.
    const text = [...document.querySelectorAll("p,span,div")]
      .filter((element) => !element.children.length && visible(element))
      .map((element) => element.textContent.trim()).join(" ");
    if (/二维码已(?:过期|失效)|二维码过期|二维码失效/.test(text)) return report("expired");
    if (/扫码成功|请在手机上确认|请在手机端确认/.test(text)) return report("confirming");
    if (/安全验证|拖动滑块|短信验证|身份验证/.test(text)) return report("action_required");

    let image;
    if (origin === "https://creator.douyin.com") {
      image = first([
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
      ]);
      if (!image && !switched) {
        const tab = [...document.querySelectorAll("button,div,span")].find(
          (element) => !element.children.length && element.textContent.trim() === "扫码登录" && visible(element),
        );
        if (tab) { switched = true; tab.click(); }
      }
    } else if (origin === "https://creator.xiaohongshu.com") {
      const box = first(["div[class*='login-box']"]);
      if (box) {
        const label = [...box.querySelectorAll("span,div,p")].find(
          (element) => !element.children.length && /APP扫一扫登录|扫一扫/.test(element.textContent),
        );
        if (label) {
          // Upstream uses the QR region following the scan-login label.
          for (const node of [label, label.parentElement]) {
            let sibling = node?.nextElementSibling;
            while (sibling && !image) {
              image = first(["img:not(.css-wemwzq)"], sibling);
              sibling = sibling.nextElementSibling;
            }
          }
          image ||= first(['img[src^="data:image"]', 'img[class*="qrcode"]'], box);
        } else if (!switched) {
          const toggle = first(["img.css-wemwzq"], box);
          if (toggle) { switched = true; toggle.click(); }
        }
      }
    } else {
      image = first(["div.login-qrcode-wrap img.qrcode", "div.qrcode-wrap img.qrcode", "img.qrcode", "img.js_qrcode_img"]);
      if (!image && bridge && document.contains(bridge.frame) && visible(bridge.frame) && Date.now() - bridge.at < 4000) {
        return report(bridge.phase, bridge.image);
      }
    }
    if (image) {
      const data = readImage(image);
      return report(data ? "qr_ready" : "action_required", data);
    }
    report("loading");
  }
  const read = () => {
    try { scan(); } catch { report("action_required"); }
    return window.__xiangshuPublishLogin;
  };
  // Hidden WebView2 windows can throttle timers. Rust actively reads the page
  // on every poll; request the same refresh from the trusted WeChat QR frame.
  if (!child) window.__xiangshuReadPublishLogin = () => {
    if (origin === channels) {
      for (const frame of document.querySelectorAll("iframe")) {
        try {
          const url = new URL(frame.getAttribute("src"), location.href);
          if (qrFrame(url)) frame.contentWindow?.postMessage({ type: "xiangshu-read-publish-qr" }, url.origin);
        } catch { /* Ignore unrelated frames. */ }
      }
    }
    return read();
  };
  else window.addEventListener("message", (event) => {
    if (event.origin === channels && event.source === window.top && event.data?.type === "xiangshu-read-publish-qr") read();
  });
  const tick = () => {
    read();
    setTimeout(tick, 1000);
  };
  report("loading");
  setTimeout(tick, 0);
})();
