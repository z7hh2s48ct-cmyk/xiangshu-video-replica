import { CustomersPage } from "./CustomersPage";

export function CustomersManagementPage({
  operatorId = "standalone-admin",
  initialIntent = "",
  readOnly = false,
}: {
  operatorId?: string;
  initialIntent?: string;
  readOnly?: boolean;
}) {
  return (
    <CustomersPage
      embedded
      initialIntent={initialIntent}
      operatorId={operatorId}
      readOnly={readOnly}
    />
  );
}
