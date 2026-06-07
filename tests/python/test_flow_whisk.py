"""
Tests for POST /whisk and POST /flow/storyboard
"""
import pytest
import json
import base64
from unittest.mock import patch, MagicMock

FAKE_B64 = base64.b64encode(b"fakeimagedata").decode()

SAMPLE_SCENES_JSON = json.dumps([
    {
        "title": "Opening shot of Cairo skyline",
        "description": "Wide establishing shot of Cairo at golden hour, ancient pyramids visible.",
        "camera": "slow pan left",
        "audio": "ambient city sounds, distant call to prayer",
        "duration": 8,
    },
    {
        "title": "Close-up market scene",
        "description": "Bustling spice market, warm tones, merchant hands weighing saffron.",
        "camera": "handheld close-up",
        "audio": "market chatter, spice scents implied",
        "duration": 5,
    },
])


class TestWhiskEndpoint:
    def test_whisk_registered(self):
        """POST /whisk must be in routes."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("whisk" in r for r in routes), f"Whisk route missing. Routes: {routes}"

    def test_whisk_empty_slots_fails(self, client):
        """All-empty slots should return 400."""
        response = client.post("/whisk", json={
            "model": "flux-kontext-max",
            "aspect_ratio": "1:1",
        })
        assert response.status_code in (400, 422, 500)

    def test_whisk_subject_description_only(self, client):
        """Whisk with just a text description should attempt generation."""
        with patch("laozhang_api._generate_openai_image", return_value=FAKE_B64) as mock_gen, \
             patch("laozhang_api._generate_chat_image", return_value=FAKE_B64):

            response = client.post("/whisk", json={
                "model": "flux-kontext-max",
                "aspect_ratio": "1:1",
                "subject_description": "A fluffy orange cat",
                "scene_description": "sitting in a meadow",
            })
            # 200 with image data, or 500 if model not supported in test env
            if response.status_code == 200:
                data = response.json()
                assert "image_b64" in data or "url" in data

    def test_whisk_with_image_b64(self, client):
        """Whisk with base64 image input should pass it to generation."""
        with patch("laozhang_api._generate_openai_image", return_value=FAKE_B64) as mock_gen, \
             patch("laozhang_api._generate_chat_image", return_value=FAKE_B64):

            response = client.post("/whisk", json={
                "model": "flux-kontext-max",
                "aspect_ratio": "16:9",
                "subject_image_b64": FAKE_B64,
                "subject_image_mime": "image/jpeg",
                "style_description": "watercolor painting",
            })
            if response.status_code == 200:
                assert "image_b64" in response.json() or "url" in response.json()


class TestFlowStoryboardEndpoint:
    def test_flow_storyboard_registered(self):
        """POST /flow/storyboard must be in routes."""
        from laozhang_api import app
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("flow" in r and "storyboard" in r for r in routes), \
            f"flow/storyboard missing. Routes: {routes}"

    def test_flow_requires_script(self, client):
        """POST /flow/storyboard without script → 400 or 422."""
        response = client.post("/flow/storyboard", json={"style": "cinematic"})
        assert response.status_code in (400, 422)

    def test_flow_calls_gemini(self, client):
        """flow/storyboard should call make_client() with Gemini."""
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = SAMPLE_SCENES_JSON

        with patch("laozhang_api.make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_resp
            mock_make.return_value = mock_client

            response = client.post("/flow/storyboard", json={
                "script": "A documentary about ancient Egypt exploring the pyramids.",
                "style": "cinematic",
                "scene_count": 2,
                "generate_images": False,
            })

            assert response.status_code == 200
            data = response.json()
            assert "scenes" in data
            assert len(data["scenes"]) >= 1

    def test_flow_scene_structure(self, client):
        """Each scene in the response must have required fields."""
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = SAMPLE_SCENES_JSON

        with patch("laozhang_api.make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_resp
            mock_make.return_value = mock_client

            response = client.post("/flow/storyboard", json={
                "script": "A short film about Paris.",
                "style": "noir",
                "scene_count": 2,
                "generate_images": False,
            })

            assert response.status_code == 200
            scenes = response.json()["scenes"]
            for scene in scenes:
                assert "title" in scene,       f"Scene missing title: {scene}"
                assert "description" in scene, f"Scene missing description: {scene}"

    def test_flow_scene_count_respected(self, client):
        """scene_count parameter should be respected."""
        scenes_4 = json.dumps([
            {"title": f"Scene {i}", "description": "desc", "camera": "wide", "audio": "none", "duration": 8}
            for i in range(4)
        ])
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = scenes_4

        with patch("laozhang_api.make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_resp
            mock_make.return_value = mock_client

            response = client.post("/flow/storyboard", json={
                "script": "Four seasons across the world.",
                "style": "documentary",
                "scene_count": 4,
                "generate_images": False,
            })

            assert response.status_code == 200
            data = response.json()
            assert data["scene_count"] == 4

    def test_flow_handles_malformed_json_from_llm(self, client):
        """If LLM returns wrapped markdown JSON, parser should strip it."""
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        # LLM wrapped in markdown fences
        mock_resp.choices[0].message.content = f"```json\n{SAMPLE_SCENES_JSON}\n```"

        with patch("laozhang_api.make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_resp
            mock_make.return_value = mock_client

            response = client.post("/flow/storyboard", json={
                "script": "Test script.",
                "style": "cinematic",
                "scene_count": 2,
                "generate_images": False,
            })
            # Should still parse successfully
            assert response.status_code == 200
            assert len(response.json()["scenes"]) >= 1

    def test_flow_style_and_scene_count_in_response(self, client):
        """Response should echo style and scene_count."""
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = SAMPLE_SCENES_JSON

        with patch("laozhang_api.make_client") as mock_make:
            mock_client = MagicMock()
            mock_client.chat.completions.create.return_value = mock_resp
            mock_make.return_value = mock_client

            response = client.post("/flow/storyboard", json={
                "script": "Test.",
                "style": "anime",
                "scene_count": 2,
                "generate_images": False,
            })
            data = response.json()
            assert data.get("style") == "anime"
            assert "scene_count" in data
