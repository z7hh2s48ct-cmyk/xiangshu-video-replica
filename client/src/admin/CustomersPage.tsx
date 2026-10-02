import { CustomerList, type CustomerListProps } from "./CustomerList";

export function CustomersPage(props: CustomerListProps = {}) {
  return <CustomerList {...props} />;
}
