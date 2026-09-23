export function PairingApprovalCard({
  pairing,
  onApprove,
  onDelete,
  onReject,
}: {
  pairing: {
    id: string;
    deviceFingerprint: string;
    slotNo?: number;
    createdAt: string;
  };
  onApprove: (pairingId: string) => void;
  onDelete: (pairingId: string) => void;
  onReject: () => void;
}): React.JSX.Element {
  return (
    <article className="pairing-approval-card">
      <div>
        <span className="status-badge">
          待确认{pairing.slotNo ? ` · 设备 ${pairing.slotNo}` : ""}
        </span>
        <h3>新的设备绑定请求</h3>
        <p>{pairing.deviceFingerprint}</p>
        <small>{new Date(pairing.createdAt).toLocaleString("zh-CN")}</small>
      </div>
      <div className="card-actions">
        <button
          className="danger-button"
          onClick={() => onDelete(pairing.id)}
          type="button"
        >
          删除无效请求
        </button>
        <button className="secondary-button" onClick={onReject} type="button">
          暂不处理
        </button>
        <button onClick={() => onApprove(pairing.id)} type="button">
          确认绑定
        </button>
      </div>
    </article>
  );
}
