import "./brand-identity.css";

export function BrandIdentity() {
  return (
    <span className="brand-identity">
      <img
        src="/studio/logo-mark.svg"
        alt="众墅之家"
        width={50.4}
        height={43.2}
      />
      <span className="brand-identity__text">
        <strong>众墅之家</strong>
        <span aria-hidden="true">｜</span>
        <span>AI 即创</span>
      </span>
    </span>
  );
}
