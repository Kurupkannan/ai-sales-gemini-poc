"""Tests for Exotel outbound test call request construction (mocked external calls)."""

from unittest.mock import AsyncMock, patch
import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


@pytest.mark.asyncio
async def test_outbound_missing_public_base_url():
    original_url = settings.PUBLIC_BASE_URL
    try:
        settings.PUBLIC_BASE_URL = ""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/test-call", json={"to_number": "+919876543210"})
            assert response.status_code == 400
            assert "PUBLIC_BASE_URL is not configured" in response.json()["detail"]
    finally:
        settings.PUBLIC_BASE_URL = original_url


@pytest.mark.asyncio
async def test_exotel_outbound_request_construction():
    original_url = settings.PUBLIC_BASE_URL
    original_key = settings.EXOTEL_API_KEY
    original_token = settings.EXOTEL_API_TOKEN
    original_sid = settings.EXOTEL_ACCOUNT_SID
    original_exophone = settings.EXOTEL_EXOPHONE

    try:
        settings.PUBLIC_BASE_URL = "https://myserver.ngrok-free.app"
        settings.EXOTEL_API_KEY = "test_api_key"
        settings.EXOTEL_API_TOKEN = "test_api_token"
        settings.EXOTEL_ACCOUNT_SID = "test_acc_sid"
        settings.EXOTEL_EXOPHONE = "04954269828"

        # Mock httpx response from Exotel
        mock_response = httpx.Response(
            status_code=200,
            json={"Call": {"Sid": "call_mock_12345", "Status": "in-progress"}},
            headers={"Content-Type": "application/json"},
        )

        with patch("app.main.httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_instance.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client_instance

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/test-call", json={"to_number": "+919876543210"})

                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "initiated"
                assert data["call_sid"] == "call_mock_12345"
                assert data["stream_url"] == "wss://myserver.ngrok-free.app/exotel/media"

                # Verify mock post parameters
                mock_client_instance.post.assert_awaited_once()
                call_args, call_kwargs = mock_client_instance.post.call_args
                url = call_args[0]
                assert "test_acc_sid" in url
                assert "Calls/connect" in url

                form_data = call_kwargs["data"]
                assert form_data["From"] == "+919876543210"
                assert form_data["CallerId"] == "04954269828"
                assert form_data["StreamUrl"] == "wss://myserver.ngrok-free.app/exotel/media"
                assert form_data["StreamType"] == "bidirectional"

                auth = call_kwargs["auth"]
                assert auth == ("test_api_key", "test_api_token")
    finally:
        settings.PUBLIC_BASE_URL = original_url
        settings.EXOTEL_API_KEY = original_key
        settings.EXOTEL_API_TOKEN = original_token
        settings.EXOTEL_ACCOUNT_SID = original_sid
        settings.EXOTEL_EXOPHONE = original_exophone
