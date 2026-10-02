import { DeviceManagement } from "./DeviceManagement";

export function CustomerDeviceSection({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  return <DeviceManagement key={userId} userId={userId} readOnly={readOnly} />;
}
