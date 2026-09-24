/** Session displaced notice (FE-03 / T30).
 * Shows when a user's session is replaced by another device pairing.
 */
export function SessionDisplacedNotice({
  onRestart,
}: {
  onRestart: () => void;
}): React.JSX.Element {
  return (
    <div
      className="session-displaced-notice"
      role="alert"
      aria-labelledby="displaced-title"
    >
      <header>
        <h1 id="displaced-title">Session Displaced</h1>
      </header>

      <div id="displaced-description">
        <p className="displaced-message">
          Session Replaced: Your session has been replaced by another device
          that just signed in.
        </p>

        <p className="active-device-info">The other device is now active.</p>

        <p className="instruction-message">
          To see your current session, please sign in again on the new device.
        </p>
      </div>

      <footer className="notice-actions">
        <button
          type="button"
          className="btn-primary"
          onClick={onRestart}
          aria-label="View My Session"
        >
          View My Session
        </button>
      </footer>
    </div>
  );
}
