import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, MagicMock
import json
from io import BytesIO
import zipfile

from app.main import app
from app.models.schemas import (
    AnalyzeRequest, AnalyzeResponse, RepoSummary,
    GitHubRepository, GitHubBranch
)


client = TestClient(app)


class TestRootEndpoint:
    """Test root endpoint returns correct service metadata."""

    def test_root_returns_service_info(self):
        """Root endpoint should return service metadata."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "ShipMate AI — Agentic Engineering Command Center"
        assert data["version"] == "1.0.0"
        assert data["status"] == "operational"
        assert data["docs"] == "/docs"
        assert "tagline" in data


class TestHealthCheckEndpoint:
    """Test health check endpoint."""

    def test_health_check_returns_healthy_status(self):
        """Health check should return healthy status with all agents."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "agents" in data
        assert len(data["agents"]) == 5
        expected_agents = [
            "planner",
            "repo_analyst",
            "test_generator",
            "security_guard",
            "delivery_manager"
        ]
        assert data["agents"] == expected_agents
        assert data["message"] == "All systems operational"


class TestGitHubCallbackHtml:
    """Test GitHub OAuth callback HTML page."""

    def test_github_callback_html_returns_html(self):
        """GitHub callback endpoint should return HTML content."""
        response = client.get("/github-callback.html")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        content = response.text
        assert "ShipMate AI - GitHub Authorization" in content
        assert "Authorizing with GitHub" in content
        assert "GITHUB_AUTH_SUCCESS" in content


class TestAnalyzeEndpoint:
    """Test /api/analyze endpoint with various inputs."""

    @patch("app.main.run_full_analysis")
    def test_analyze_with_valid_request(self, mock_analysis):
        """Analyze endpoint should accept valid feature request."""
        mock_response = AnalyzeResponse(
            production_readiness_score=85,
            summary="Test analysis",
            agents_output={},
            recommendations=[],
            risk_assessment="low"
        )
        mock_analysis.return_value = mock_response

        payload = {
            "feature_request": "Add user authentication to the dashboard",
            "repo_details": None
        }
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "production_readiness_score" in data
        mock_analysis.assert_called_once()

    def test_analyze_with_empty_feature_request(self):
        """Analyze endpoint should reject empty feature request."""
        payload = {
            "feature_request": "",
            "repo_details": None
        }
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == 400
        assert "at least 10 characters" in response.json()["detail"]

    def test_analyze_with_short_feature_request(self):
        """Analyze endpoint should reject feature request shorter than 10 chars."""
        payload = {
            "feature_request": "short",
            "repo_details": None
        }
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == 400
        assert "at least 10 characters" in response.json()["detail"]

    def test_analyze_with_whitespace_only_request(self):
        """Analyze endpoint should reject whitespace-only feature request."""
        payload = {
            "feature_request": "     ",
            "repo_details": None
        }
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == 400
        assert "at least 10 characters" in response.json()["detail"]

    @patch("app.main.run_full_analysis")
    def test_analyze_handles_analysis_exception(self, mock_analysis):
        """Analyze endpoint should handle exceptions from analysis service."""
        mock_analysis.side_effect = Exception("Analysis service error")

        payload = {
            "feature_request": "Add comprehensive logging to the API",
            "repo_details": None
        }
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == 500
        assert "Analysis failed" in response.json()["detail"]


class TestUploadRepoEndpoint:
    """Test /api/upload-repo endpoint."""

    def _create_zip_file(self, content: dict = None) -> bytes:
        """Helper to create a valid ZIP file."""
        if content is None:
            content = {"README.md": "# Test Repo"}
        
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for filename, file_content in content.items():
                zf.writestr(filename, file_content)
        return zip_buffer.getvalue()

    def test_upload_repo_with_no_file(self):
        """Upload endpoint should reject request with no file."""
        response = client.post("/api/upload-repo")
        assert response.status_code == 422  # FastAPI validation error

    def test_upload_repo_with_non_zip_file(self):
        """Upload endpoint should reject non-ZIP files."""
        response = client.post(
            "/api/upload-repo",
            files={"file": ("test.txt", b"not a zip", "text/plain")}
        )
        assert response.status_code == 400
        assert "Only ZIP files are supported" in response.json()["detail"]

    @patch("app.main.process_repo_zip")
    def test_upload_repo_with_valid_zip(self, mock_process):
        """Upload endpoint should accept valid ZIP file."""
        mock_summary = RepoSummary(
            repo_name="test-repo",
            tech_stack=["Python", "FastAPI"],
            file_tree="test",
            key_files={}
        )
        mock_process.return_value = mock_summary

        zip_content = self._create_zip_file()
        response = client.post(
            "/api/upload-repo",
            files={"file": ("repo.zip", zip_content, "application/zip")}
        )
        assert response.status_code == 200
        data = response.json()
        assert "repo_name" in data
        mock_process.assert_called_once()

    def test_upload_repo_with_oversized_file(self):
        """Upload endpoint should reject files larger than 50MB."""
        # Create a file larger than 50MB
        large_content = b"x" * (51 * 1024 * 1024)
        response = client.post(
            "/api/upload-repo",
            files={"file": ("large.zip", large_content, "application/zip")}
        )
        assert response.status_code == 413
        assert "File too large" in response.json()["detail"]

    @patch("app.main.process_repo_zip")
    def test_upload_repo_handles_value_error(self, mock_process):
        """Upload endpoint should handle ValueError from process_repo_zip."""
        mock_process.side_effect = ValueError("Invalid ZIP structure")

        zip_content = self._create_zip_file()
        response = client.post(
            "/api/upload-repo",
            files={"file": ("repo.zip", zip_content, "application/zip")}
        )
        assert response.status_code == 400
        assert "Invalid ZIP structure" in response.json()["detail"]

    @patch("app.main.process_repo_zip")
    def test_upload_repo_handles_generic_exception(self, mock_process):
        """Upload endpoint should handle generic exceptions from process_repo_zip."""
        mock_process.side_effect = Exception("Unexpected error")

        zip_content = self._create_zip_file()
        response = client.post(
            "/api/upload-repo",
            files={"file": ("repo.zip", zip_content, "application/zip")}
        )
        assert response.status_code == 500
        assert "Failed to process repository" in response.json()["detail"]


class TestSampleEndpoint:
    """Test /api/sample endpoint."""

    @patch("app.main.get_sample_repo")
    def test_sample_returns_repo_summary(self, mock_sample):
        """Sample endpoint should return a RepoSummary."""
        mock_summary = RepoSummary(
            repo_name="sample-ecommerce",
            tech_stack=["TypeScript", "React"],
            file_tree="sample tree",
            key_files={}
        )
        mock_sample.return_value = mock_summary

        response = client.get("/api/sample")
        assert response.status_code == 200
        data = response.json()
        assert "repo_name" in data
        assert "tech_stack" in data
        mock_sample.assert_called_once()


class TestReportExportEndpoint:
    """Test /api/reports/export endpoint."""

    def test_export_report_without_analysis_id(self):
        """Export endpoint should require analysis_id query parameter."""
        response = client.post("/api/reports/export")
        assert response.status_code == 422  # FastAPI validation error

    def test_export_report_with_valid_analysis_id(self):
        """Export endpoint should return report metadata."""
        response = client.post("/api/reports/export?analysis_id=test-123")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["analysis_id"] == "test-123"
        assert data["format"] == "markdown"
        assert "download_url" in data
        assert data["expires_in_hours"] == 24


class TestReportDownloadEndpoint:
    """Test /api/reports/{analysis_id}/download endpoint."""

    def test_download_report_with_valid_id(self):
        """Download endpoint should return report metadata."""
        response = client.get("/api/reports/test-123/download")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert data["analysis_id"] == "test-123"

    def test_download_report_with_different_ids(self):
        """Download endpoint should handle different analysis IDs."""
        test_ids = ["abc-123", "xyz-789", "report-001"]
        for test_id in test_ids:
            response = client.get(f"/api/reports/{test_id}/download")
            assert response.status_code == 200
            data = response.json()
            assert data["analysis_id"] == test_id


class TestCORSConfiguration:
    """Test CORS middleware configuration."""

    def test_cors_allows_localhost_origins(self):
        """CORS should allow localhost origins for development."""
        origins = [
            "http://localhost:5173",
            "http://localhost:5174",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
            "http://127.0.0.1:3000",
        ]
        for origin in origins:
            response = client.options(
                "/",
                headers={"Origin": origin}
            )
            # CORS preflight should be handled
            assert response.status_code in [200, 204]

    def test_cors_allows_all_methods(self):
        """CORS should allow all HTTP methods."""
        response = client.options(
            "/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST"
            }
        )
        assert response.status_code in [200, 204]


class TestEndpointIntegration:
    """Integration tests for multiple endpoints."""

    def test_health_check_before_analysis(self):
        """Should be able to check health before running analysis."""
        health_response = client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["status"] == "healthy"

    @patch("app.main.run_full_analysis")
    def test_root_and_analyze_endpoints(self, mock_analysis):
        """Root and analyze endpoints should work together."""
        mock_response = AnalyzeResponse(
            production_readiness_score=75,
            summary="Integration test",
            agents_output={},
            recommendations=[],
            risk_assessment="medium"
        )
        mock_analysis.return_value = mock_response

        # Check root
        root_response = client.get("/")
        assert root_response.status_code == 200

        # Run analysis
        payload = {
            "feature_request": "Implement OAuth2 authentication flow",
            "repo_details": None
        }
        analyze_response = client.post("/api/analyze", json=payload)
        assert analyze_response.status_code == 200


class TestErrorHandling:
    """Test error handling across endpoints."""

    def test_invalid_json_payload(self):
        """Endpoints should handle invalid JSON gracefully."""
        response = client.post(
            "/api/analyze",
            data="invalid json",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422

    def test_missing_required_fields(self):
        """Endpoints should validate required fields."""
        payload = {"repo_details": None}  # Missing feature_request
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == 422

    def test_invalid_query_parameter_type(self):
        """Endpoints should validate query parameter types."""
        response = client.post("/api/reports/export?analysis_id=123")
        # Should still work as string
        assert response.status_code == 200
