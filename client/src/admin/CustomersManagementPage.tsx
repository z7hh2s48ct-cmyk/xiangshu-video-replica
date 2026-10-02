import { CustomersPage } from "./CustomersPage";

export function CustomersManagementPage({
  operatorId = "standalone-admin",
  initialIntent = "",
  initialCustomerId = "",
  readOnly = false,
  initialListQuery,
  onScopeChange,
  onCustomer,
  onReturnToList,
}: {
  operatorId?: string;
  initialIntent?: string;
  initialCustomerId?: string;
  readOnly?: boolean;
  initialListQuery?: string;
  onScopeChange?: (scope: Record<string, string>) => void;
  onCustomer?: (id: string) => void;
  onReturnToList?: () => void;
}) {
  return (
    <CustomersPage
      embedded
      initialIntent={initialIntent}
      initialCustomerId={initialCustomerId}
      operatorId={operatorId}
      readOnly={readOnly}
      initialListQuery={initialListQuery}
      onScopeChange={onScopeChange}
      onCustomer={onCustomer}
      onReturnToList={onReturnToList}
    />
  );
}
