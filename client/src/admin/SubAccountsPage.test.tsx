/**
 * React Testing Library component tests for Sub-Account Management System
 *
 * Tests cover:
 * - SubAccountsPage rendering and interactions
 * - Form validation behavior
 * - Sorting functionality
 * - Status filtering
 */

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, type Mock, vi } from "vitest";
import { SubAccountsPage } from "./SubAccountsPage";

// Mock API functions
vi.mock("../api.admin", () => ({
  listMasterAccounts: vi.fn(),
  listSubAccounts: vi.fn(),
  createSubAccount: vi.fn(),
  updateSubAccount: vi.fn(),
  deleteSubAccount: vi.fn(),
}));

import {
  createSubAccount,
  listMasterAccounts,
  listSubAccounts,
} from "../api.admin";

const mockMasters = [
  {
    user_id: "master-1",
    display_name: "母账号 A",
    username: "master_a",
    account_type: "MASTER",
  },
  {
    user_id: "master-2",
    display_name: "母账号 B",
    username: "master_b",
    account_type: "MASTER",
  },
];

const mockSubAccounts = [
  {
    id: "sub-1",
    username: "sub_user_1",
    display_name: "子账号 1",
    parent_user_id: "master-1",
    is_active: true,
    created_at: "2026-09-19T10:00:00Z",
    updated_at: null,
  },
  {
    id: "sub-2",
    username: "sub_user_2",
    display_name: "子账号 2",
    parent_user_id: "master-1",
    is_active: false,
    created_at: "2026-09-18T10:00:00Z",
    updated_at: null,
  },
];

describe("SubAccountsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders without crashing", async () => {
    // Setup mocks
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    const { container } = render(
      <SubAccountsPage operatorId="test-operator" />,
    );
    await waitFor(() => {
      expect(container).toBeDefined();
    });
  });

  it("loads master accounts on mount", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    render(<SubAccountsPage operatorId="test-operator" />);

    await waitFor(() => {
      expect(listMasterAccounts).toHaveBeenCalledWith(100, 0);
    });
  });

  it("shows parent selector dropdown when data loads", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    render(<SubAccountsPage operatorId="test-operator" />);

    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      expect(selectElement).toBeVisible();
    });
  });

  it("displays loading skeleton initially when sub-accounts exist", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    (listSubAccounts as Mock).mockResolvedValue({
      sub_accounts: mockSubAccounts,
      total_count: 2,
    });

    const { container } = render(
      <SubAccountsPage operatorId="test-operator" />,
    );

    // Wait for parent selection and sub-accounts load
    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      fireEvent.change(selectElement, { target: { value: "master-1" } });
    });

    // Check if skeleton shows during loading
    await waitFor(
      () => {
        const tableRows = container.querySelectorAll(".data-table tbody tr");
        expect(tableRows.length).toBeGreaterThan(0);
      },
      { timeout: 2000 },
    );
  });

  it("allows creating a new sub-account", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    (listSubAccounts as Mock).mockResolvedValue({
      sub_accounts: [],
      total_count: 0,
    });

    (createSubAccount as Mock).mockResolvedValue({
      id: "sub-new",
      username: "new_sub",
      display_name: "新子账号",
      account_type: "SUB",
      parent_user_id: "master-1",
      is_active: true,
      created_at: "2026-09-19T12:00:00Z",
      updated_at: null,
    });

    render(<SubAccountsPage operatorId="test-operator" />);

    // Select parent
    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      fireEvent.change(selectElement, { target: { value: "master-1" } });
    });

    // Fill form
    const usernameInput = screen.getByPlaceholderText(/用户名/i);
    const displayNameInput = screen.getByPlaceholderText(/显示名称/i);
    const reasonTextarea = screen.getByPlaceholderText(/创建原因/i);

    fireEvent.change(usernameInput, { target: { value: "new_sub" } });
    fireEvent.change(displayNameInput, { target: { value: "新子账号" } });
    fireEvent.change(reasonTextarea, { target: { value: "用于测试" } });

    // Submit
    const submitButton = screen.getByRole("button", { name: /创建子账号/i });
    fireEvent.click(submitButton);

    // Verify API call was made
    await waitFor(() => {
      expect(createSubAccount).toHaveBeenCalledWith({
        username: "new_sub",
        display_name: "新子账号",
        parent_user_id: "master-1",
        reason: "用于测试",
      });
    });
  });

  it("validates username uniqueness in real-time", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    // Return existing username
    (listSubAccounts as Mock).mockResolvedValue({
      sub_accounts: [
        ...mockSubAccounts,
        { ...mockSubAccounts[0], username: "duplicate" },
      ],
      total_count: 3,
    });

    render(<SubAccountsPage operatorId="test-operator" />);

    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      fireEvent.change(selectElement, { target: { value: "master-1" } });
    });

    // Type existing username
    const usernameInput = screen.getByPlaceholderText(/用户名/i);
    fireEvent.change(usernameInput, { target: { value: "sub_user_1" } });

    // Wait for debounce delay
    await waitFor(
      () => {
        const errorElement = screen.queryByRole("alert");
        expect(errorElement?.textContent).toContain("已被使用");
      },
      { timeout: 2000 },
    );
  });

  it("applies status filter correctly", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    (listSubAccounts as Mock).mockResolvedValue({
      sub_accounts: mockSubAccounts,
      total_count: 2,
    });

    render(<SubAccountsPage operatorId="test-operator" />);

    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      fireEvent.change(selectElement, { target: { value: "master-1" } });
    });

    // Wait for table to populate
    await waitFor(
      () => {
        const rows = screen.getAllByRole("row");
        expect(rows.length).toBeGreaterThanOrEqual(2);
      },
      { timeout: 2000 },
    );

    // Change filter to active only
    const filterSelect = screen.getByLabelText(/状态筛选/i);
    fireEvent.change(filterSelect, { target: { value: "active" } });

    // Should show only active sub-accounts
    const visibleRows = screen.getAllByRole("row");
    expect(
      visibleRows.some((row) => row.textContent.includes("子账号 1")),
    ).toBe(true);
    expect(
      visibleRows.some((row) => row.textContent.includes("子账号 2")),
    ).toBe(false);
  });

  it("sorts by created_at descending by default", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    (listSubAccounts as Mock).mockResolvedValue({
      sub_accounts: mockSubAccounts,
      total_count: 2,
    });

    const { container } = render(
      <SubAccountsPage operatorId="test-operator" />,
    );

    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      fireEvent.change(selectElement, { target: { value: "master-1" } });
    });

    await waitFor(
      () => {
        const rows = within(container).getAllByRole("row");
        // First data row should be the most recent (sub-1)
        expect(rows[1]?.textContent).toContain("子账号 1");
      },
      { timeout: 2000 },
    );
  });

  it("handles empty state when no sub-accounts exist", async () => {
    (listMasterAccounts as Mock).mockResolvedValue({
      items: mockMasters,
      total: 2,
      limit: 100,
      offset: 0,
    });

    (listSubAccounts as Mock).mockResolvedValue({
      sub_accounts: [],
      total_count: 0,
    });

    render(<SubAccountsPage operatorId="test-operator" />);

    await waitFor(() => {
      const selectElement = screen.getByRole("combobox");
      fireEvent.change(selectElement, { target: { value: "master-1" } });
    });

    await waitFor(
      () => {
        const emptyState = screen.getByText("暂无子账号");
        expect(emptyState).toBeInTheDocument();
      },
      { timeout: 2000 },
    );
  });
});
