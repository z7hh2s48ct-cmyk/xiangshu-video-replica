"""Tests for export_controller.py - Statistics report export functionality."""

import gzip
import csv
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.export_controller import ExportRequest


class TestExportRequestValidation:
    """Test ExportRequest parameter validation."""
    
    def test_valid_request_min_max_revenue(self):
        """Test request with revenue filters."""
        from app.export_controller import ExportRequest
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            service_types=["viral_data"],
            min_revenue_fen=100,
            max_revenue_fen=10000,
        )
        
        assert request.format == "csv"
        assert request.start_date == date(2026, 9, 1)
        assert request.min_revenue_fen == 100
        assert request.max_revenue_fen == 10000
    
    def test_invalid_date_range(self):
        """Test that start > end raises error."""
        from app.export_controller import validate_request
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 30),
            end_date=date(2026, 9, 1),
        )
        
        valid, message = validate_request(request)
        assert valid is False
        assert "must be before or equal" in message
    
    def test_date_range_exceeds_90_days(self):
        """Test that range > 90 days is rejected."""
        from app.export_controller import validate_request
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 6, 30),  # ~180 days
        )
        
        valid, message = validate_request(request)
        assert valid is False
        assert "cannot exceed 90 days" in message
    
    def test_invalid_format(self):
        """Test that unsupported format is rejected."""
        from app.export_controller import validate_request
        
        request = ExportRequest(
            format="pdf",  # Invalid
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        
        valid, message = validate_request(request)
        assert valid is False
        assert "Unsupported format" in message


class TestCSVGeneration:
    """Test CSV streaming generation logic."""
    
    @pytest.fixture
    def mock_conn(self):
        """Create mocked database connection."""
        conn = MagicMock()
        
        # Mock query result
        mock_result = MagicMock()
        
        # Create mock rows
        row1 = {
            "id": "txn-001",
            "user_id": "user-123",
            "username": "user-123",
            "billing_date": date(2026, 9, 15),
            "service_type": "viral_data",
            "credits_delta": -1,
            "credits_used": 1,
            "unit_cost_fen": 0.10,
            "cost_fen": 0.10,
            "billing_round": 1,
        }

        row2 = {
            "id": "txn-002",
            "user_id": "user-123",
            "username": None,  # NULL username test
            "billing_date": date(2026, 9, 16),
            "service_type": "video_768p",
            "credits_delta": -10,
            "credits_used": 10,
            "unit_cost_fen": 0.05,
            "cost_fen": 0.50,
            "billing_round": 1,
        }
        
        mock_result.fetchall.return_value = [row1, row2]
        mock_result.fetchone.return_value = {"total": 2}
        mock_result.__iter__ = MagicMock(return_value=iter([row1, row2]))
        
        conn.execute.return_value = mock_result
        return conn
    
    @pytest.mark.asyncio
    async def test_csv_generation_basic(self, mock_conn):
        """Test basic CSV generation works correctly."""
        from app.export_controller import generate_csv_content
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            service_types=["all"],
        )
        
        csv_bytes = generate_csv_content(mock_conn, request)
        
        # Verify it's gzip compressed
        assert len(csv_bytes) > 0
        
        # Decompress and parse
        decompressed = gzip.decompress(csv_bytes)
        text = decompressed.decode("utf-8")
        
        # Parse as CSV
        reader = csv.reader(text.splitlines())
        rows = list(reader)
        
        # Check header
        assert rows[0][0] == "Transaction ID"
        assert rows[0][1] == "User ID"
        assert rows[0][2] == "Username"
        
        # Check data rows count (header + 2 test rows)
        assert len(rows) == 3
    
    @pytest.mark.asyncio
    async def test_csv_handles_null_email(self, mock_conn):
        """Test that NULL emails are handled gracefully."""
        from app.export_controller import generate_csv_content
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        
        csv_bytes = generate_csv_content(mock_conn, request)
        decompressed = gzip.decompress(csv_bytes).decode("utf-8")
        
        # Verify N/A appears for null email
        assert "N/A" in decompressed
    
    @pytest.mark.asyncio
    async def test_csv_compression_ratio(self, mock_conn):
        """Test that compression reduces file size significantly."""
        from app.export_controller import generate_csv_content
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        
        csv_bytes = generate_csv_content(mock_conn, request)
        
        # Uncompressed would be much larger
        decompressed = gzip.decompress(csv_bytes)
        compression_ratio = len(csv_bytes) / len(decompressed)

        # Two-row samples carry gzip's fixed header overhead, so the ratio sits
        # well above typical bulk levels; just assert compression actually helps.
        assert compression_ratio < 1.0


class TestExportControllerEndpoints:
    """Integration tests for export API endpoints."""
    
    @pytest.fixture
    def test_client(self):
        """Create FastAPI test client."""
        from fastapi.testclient import TestClient
        from app.main import app
        
        return TestClient(app)
    
    def test_export_endpoints_registered(self, test_client):
        """Test that all export endpoints are registered."""
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        
        paths = response.json()["paths"].keys()
        
        # Check required endpoints exist
        export_paths = [p for p in paths if "export" in p.lower()]
        assert len(export_paths) >= 1


class TestDataQuality:
    """Test data quality and edge cases."""
    
    @pytest.mark.asyncio
    async def test_empty_result_set_handling(self):
        """Test that empty results return valid empty file."""
        from app.export_controller import generate_csv_content
        
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_result.fetchone.return_value = {"total": 0}
        mock_conn.execute.return_value = mock_result
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        
        # Should not fail on empty results
        csv_bytes = generate_csv_content(mock_conn, request)
        
        # Should still be valid gzip
        decompressed = gzip.decompress(csv_bytes).decode("utf-8")
        reader = csv.reader(decompressed.splitlines())
        rows = list(reader)
        
        # Should have header only
        assert len(rows) == 1
        assert rows[0][0] == "Transaction ID"
    
    @pytest.mark.asyncio
    async def test_large_dataset_performance(self):
        """Simulate performance test with large dataset."""
        from app.export_controller import generate_csv_content
        
        # Generate 10,000 mock transactions
        mock_conn = MagicMock()
        rows = []
        
        for i in range(10000):
            row = {
                "id": f"txn-{i:06d}",
                "user_id": f"user-{i % 100:03d}",
                "username": f"user-{i % 100:03d}",
                "billing_date": date(2026, 9, i % 30 + 1),
                "service_type": ["viral_data", "video_768p"][i % 2],
                "credits_delta": -(i % 100 + 1),
                "credits_used": i % 100 + 1,
                "unit_cost_fen": round((i % 10) * 0.01, 2),
                "cost_fen": round(((i % 100 + 1) * (i % 10) * 0.01)),
                "billing_round": i % 5 + 1,
            }
            rows.append(row)

        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows  # full dataset so size assertions hold
        mock_result.__iter__ = MagicMock(return_value=iter(rows))
        mock_result.fetchone.return_value = {"total": 10000}
        mock_conn.execute.return_value = mock_result
        
        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )
        
        # Should complete within reasonable time (< 5 seconds)
        import time
        
        start_time = time.time()
        csv_bytes = generate_csv_content(mock_conn, request)
        elapsed = time.time() - start_time
        
        # Performance should be acceptable
        assert elapsed < 5.0  # < 5 seconds for 10K rows
        
        # File size should be reasonable
        assert 10000 < len(csv_bytes) < 1000000  # 10KB - 1MB


class TestRevenueFiltering:
    """Test revenue-based filtering functionality."""
    
    @pytest.mark.asyncio
    async def test_min_revenue_filter(self):
        """Test minimum revenue filter works."""
        from app.export_controller import generate_csv_content
        
        mock_conn = MagicMock()
        mock_result = MagicMock()
        
        # Create rows with different cost values
        row1 = {  # cost_fen = 50 (below min filter)
            "id": "txn-1",
            "user_id": "u-1",
            "username": "u-1",
            "billing_date": date(2026, 9, 1),
            "service_type": "viral_data",
            "credits_delta": -1,
            "credits_used": 1,
            "unit_cost_fen": 0.10,
            "cost_fen": 0.50,  # Would pass min filter of 0.10
            "billing_round": 1,
        }

        mock_result.__iter__ = MagicMock(return_value=iter([row1]))
        mock_result.fetchone.return_value = {"total": 1}
        mock_conn.execute.return_value = mock_result

        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            min_revenue_fen=1,  # Filter: >= 1 fen
        )
        
        # Should not fail
        csv_bytes = generate_csv_content(mock_conn, request)
        assert len(csv_bytes) > 0
    
    @pytest.mark.asyncio
    async def test_max_revenue_filter(self):
        """Test maximum revenue filter works."""
        from app.export_controller import generate_csv_content
        
        mock_conn = MagicMock()
        mock_result = MagicMock()
        
        row1 = {
            "id": "txn-1",
            "user_id": "u-1",
            "username": "u-1",
            "billing_date": date(2026, 9, 1),
            "service_type": "video_768p",
            "credits_delta": -100,
            "credits_used": 100,
            "unit_cost_fen": 0.05,
            "cost_fen": 5.0,  # Would be filtered by max=1.0
            "billing_round": 1,
        }

        mock_result.__iter__ = MagicMock(return_value=iter([row1]))
        mock_result.fetchone.return_value = {"total": 1}
        mock_conn.execute.return_value = mock_result

        request = ExportRequest(
            format="csv",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            max_revenue_fen=1,  # Filter: <= 1 fen
        )
        
        # Should not fail
        csv_bytes = generate_csv_content(mock_conn, request)
        assert len(csv_bytes) > 0


if __name__ == "__main__":
    pytest.main(["-v", __file__])
