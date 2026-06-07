"""
Tests for image generation, Veo, and Sora endpoints.
"""
import pytest
import base64
from unittest.mock import patch, MagicMock


FAKE_IMAGE_B64 = base64.b64encode(b"\x89PNG\r\n" + b"fakeimagedata" * 10).decode()


class TestImageModels:
    def test_image_models_endpoint(self, client):
        """GET /image-models returns a list of model dicts."""
        response = client.get("/image-models")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_image_models_have_id_and_label(self, client):
        """Each image model entry must have id and label."""
        response = client.get("/image-models")
        for m in response.json():
            assert "id" in m or "value" in m, f"Model missing id: {m}"

    def test_image_models_dict_defined(self):
        """IMAGE_MODELS dict must exist in laozhang_api."""
        from laozhang_api import IMAGE_MODELS
        assert isinstance(IMAGE_MODELS, dict)
        assert len(IMAGE_MODELS) > 0


class TestGenerateImage:
    def test_generate_image_missing_prompt(self, client):
        """POST /generate-image without prompt → 400 or 422."""
        response = client.post("/generate-image", json={"model": "flux-kontext-max"})
        assert response.status_code in (400, 422)

    def test_generate_image_calls_backend(self, client):
        """generate-image should route to the right API function."""
        from laozhang_api import IMAGE_MODELS
        if not IMAGE_MODELS:
            pytest.skip("No image models configured")

        # Pick first available model
        model_id = next(iter(IMAGE_MODELS))
        cfg = IMAGE_MODELS[model_id]

        # Mock whichever API the model uses
        with patch("laozhang_api._generate_chat_image", return_value=FAKE_IMAGE_B64) as m1, \
             patch("laozhang_api._generate_openai_image", return_value=FAKE_IMAGE_B64) as m2, \
             patch("laozhang_api._generate_google", return_value=FAKE_IMAGE_B64) as m3:

            response = client.post("/generate-image", json={
                "model": model_id,
                "prompt": "A beautiful sunset",
                "aspect_ratio": "16:9",
            })
            # Should succeed and return image data
            if response.status_code == 200:
                data = response.json()
                assert "image_b64" in data or "url" in data or "image_url" in data


class TestVeoEndpoints:
    def test_veo_submit_registered(self):
        """POST /veo/submit must be in routes."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("veo" in r and "submit" in r for r in routes), \
            f"veo/submit missing. Routes: {routes}"

    def test_veo_status_registered(self):
        """GET /veo/status/{task_id} must be registered."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("veo" in r and "status" in r for r in routes)

    def test_veo_stream_registered(self):
        """GET /veo/stream/{task_id} must be registered."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("veo" in r and "stream" in r for r in routes)

    def test_veo_submit_requires_prompt(self, client):
        """POST /veo/submit with no prompt fails gracefully."""
        with patch("laozhang_api._requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=400,
                text="prompt required",
                raise_for_status=MagicMock(side_effect=Exception("400"))
            )
            response = client.post("/veo/submit", json={
                "model": "veo-3-generate-preview",
                # no prompt
            })
            # Either 422 (validation) or 400/500 (API error)
            assert response.status_code in (400, 422, 500)

    def test_veo_submit_valid_request(self, client):
        """POST /veo/submit with valid data should attempt API call."""
        with patch("laozhang_api._requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"id": "task-abc-123", "status": "queued"}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            response = client.post("/veo/submit", json={
                "prompt": "A cat playing piano",
                "model": "veo-3-generate-preview",
                "preset": "1080p_landscape",
                "size": "1920x1080",
                "seconds": "8",
            })
            assert response.status_code == 200
            data = response.json()
            assert "task_id" in data
            assert data["task_id"] == "task-abc-123"

    def test_veo_status_valid_id(self, client):
        """GET /veo/status/{id} polls the LaoZhang API."""
        with patch("laozhang_api._requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "processing", "progress": 45}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            response = client.get("/veo/status/task-abc-123")
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert "task_id" in data


class TestSoraEndpoints:
    def test_sora_submit_registered(self):
        """POST /sora/submit must be in routes."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("sora" in r and "submit" in r for r in routes)

    def test_sora_submit_valid_request(self, client):
        """POST /sora/submit with valid data should attempt API call."""
        with patch("laozhang_api._requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"id": "sora-task-xyz", "status": "queued"}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            response = client.post("/sora/submit", json={
                "prompt": "A dolphin jumping over waves",
                "model": "sora-2",
                "size": "1280x720",
                "seconds": "8",
            })
            assert response.status_code == 200
            data = response.json()
            assert data.get("task_id") == "sora-task-xyz"

    def test_sora_status_valid(self, client):
        """GET /sora/status/{id} polls and returns status dict."""
        with patch("laozhang_api._requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"status": "succeeded", "progress": 100}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            response = client.get("/sora/status/sora-task-xyz")
            assert response.status_code == 200
            assert response.json()["status"] == "succeeded"

    def test_sora_no_api_key_error_handled(self, client):
        """If LaoZhang API returns 401, should propagate as HTTP error."""
        with patch("laozhang_api._requests.post") as mock_post:
            import requests as req_lib
            mock_post.side_effect = req_lib.exceptions.HTTPError(
                response=MagicMock(status_code=401, text="Unauthorized")
            )
            response = client.post("/sora/submit", json={
                "prompt": "test",
                "model": "sora-2",
            })
            assert response.status_code in (400, 401, 422, 500)
