import { DeviceManagement } from "./DeviceManagement";

export function SessionsPage({
  userId,
  readOnly = false,
  onCustomerChange,
}: {
  userId?: string;
  readOnly?: boolean;
  onCustomerChange?: (userId: string | undefined) => void;
}) {
  return (
    <DeviceManagement
      key={userId ?? "all"}
      userId={userId}
      global
      readOnly={readOnly}
      onCustomerChange={onCustomerChange}
    />
  );
}
