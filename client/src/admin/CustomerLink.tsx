export function CustomerLink({
  userId,
  company,
  username,
  onCustomer,
}: {
  userId?: string;
  company?: string;
  username: string;
  onCustomer?: (id: string) => void;
}) {
  const title = company?.trim() || "未填写";
  const content = (
    <>
      <strong>{title}</strong>
      <small>{username}</small>
    </>
  );
  return userId ? (
    <button
      className="customer-identity-link"
      type="button"
      onClick={() => {
        if (onCustomer) onCustomer(userId);
        else
          window.location.hash = `#admin/customersMgmt?intent=customer&userId=${encodeURIComponent(userId)}`;
      }}
    >
      {content}
    </button>
  ) : (
    <span className="customer-identity-link">{content}</span>
  );
}
