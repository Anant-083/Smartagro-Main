import base64
import io
import unittest
from unittest.mock import patch

from flask import Flask
from PIL import Image

import offline_diagnosis
from routes import diagnose as diagnose_route
from routes.diagnose import diagnose_bp


class _FakeInput:
    name = "input"


class _FakeSession:
    def __init__(self, logits):
        self.logits = logits

    def get_inputs(self):
        return [_FakeInput()]

    def run(self, _outputs, _inputs):
        return [self.logits]


def _encoded_test_image():
    buffer = io.BytesIO()
    Image.new("RGB", (240, 240), color=(30, 120, 40)).save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class OfflineDiagnosisTests(unittest.TestCase):
    def test_plant_check_logs_model_and_true_result(self):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"is_plant":true,"reason":"leaf visible"}'
                }
            }]
        }
        with (
            self.assertLogs("smartagro", level="INFO") as captured,
            patch.object(diagnose_route, "GROQ_API_KEY", "configured"),
            patch.object(diagnose_route.requests, "post", return_value=response) as post,
        ):
            is_plant, reason = diagnose_route.ai_is_crop_image("encoded-image")

        self.assertTrue(is_plant)
        self.assertIsNone(reason)
        self.assertEqual(post.call_args.kwargs["json"]["model"], diagnose_route.vision_models[0])
        self.assertTrue(any(
            "[Diagnosis step 1/5] Groq plant-image check: calling model="
            f"{diagnose_route.vision_models[0]}" in message
            for message in captured.output
        ))
        self.assertTrue(any(
            "[Diagnosis step 1/5] Groq plant-image check result: PLANT" in message
            for message in captured.output
        ))

    def test_plant_check_logs_false_result_and_rejects_image(self):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"is_plant":false,"reason":"no plant visible"}'
                }
            }]
        }
        with (
            self.assertLogs("smartagro", level="INFO") as captured,
            patch.object(diagnose_route, "GROQ_API_KEY", "configured"),
            patch.object(diagnose_route.requests, "post", return_value=response),
        ):
            is_plant, reason = diagnose_route.ai_is_crop_image("encoded-image")

        self.assertFalse(is_plant)
        self.assertEqual(reason, "no plant visible")
        self.assertTrue(any(
            "[Diagnosis step 1/5] Groq plant-image check result: NOT A PLANT"
            in message
            for message in captured.output
        ))

    def test_preprocessing_matches_model_input_shape(self):
        tensor = offline_diagnosis._decode_image(_encoded_test_image())

        self.assertEqual(tensor.shape, (1, 3, 224, 224))
        self.assertEqual(tensor.dtype.name, "float32")

    def test_high_score_returns_classification_without_treatment_advice(self):
        logits = [[0.0] * len(offline_diagnosis.CLASS_NAMES)]
        logits[0][2] = 10.0
        with patch.object(offline_diagnosis, "_get_session", return_value=_FakeSession(logits)):
            result = offline_diagnosis.predict_offline_diagnosis(_encoded_test_image())

        self.assertEqual(result["disease"], "Potato - Early Blight")
        self.assertGreaterEqual(result["confidence"], 60)
        self.assertTrue(result["offline_fallback"])
        self.assertEqual(result["eco_remedies"], [])
        self.assertEqual(result["chemical_remedies"], [])

    def test_low_score_does_not_assign_a_disease(self):
        logits = [[0.0] * len(offline_diagnosis.CLASS_NAMES)]
        with patch.object(offline_diagnosis, "_get_session", return_value=_FakeSession(logits)):
            result = offline_diagnosis.predict_offline_diagnosis(_encoded_test_image())

        self.assertEqual(result["disease"], "Unable to identify from supported classes")
        self.assertLess(result["confidence"], 60)

    def test_gemma_request_uses_google_model_api_and_parses_diagnosis(self):
        response = unittest.mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": '{"disease":"Tomato Early Blight","confidence":88}'}]
                }
            }]
        }
        with (
            patch.object(diagnose_route, "GEMMA_DIAGNOSE_KEY", "test-key"),
            patch.object(diagnose_route, "GEMMA_DIAGNOSIS_MODEL", "gemma-test-model"),
            patch.object(diagnose_route.requests, "post", return_value=response) as post,
        ):
            result = diagnose_route._run_gemma_pass("encoded-image", "diagnosis prompt", "system")

        self.assertEqual(result["disease"], "Tomato Early Blight")
        args, kwargs = post.call_args
        self.assertIn("/models/gemma-test-model:generateContent", args[0])
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(
            kwargs["json"]["contents"][0]["parts"][0]["inline_data"]["data"],
            "encoded-image",
        )

    def test_hosted_providers_are_tried_before_offline_fallback(self):
        app = Flask(__name__)
        app.register_blueprint(diagnose_bp)
        offline_result = {
            "disease": "Tomato Early Blight",
            "confidence": 90,
            "offline_fallback": True,
            "offline_notice": "Offline classifier notice.",
            "eco_remedies": [],
            "chemical_remedies": [],
            "prevention": [],
            "_english_source": {"disease": "Tomato Early Blight"},
        }
        with (
            self.assertLogs("smartagro", level="INFO") as captured,
            patch.object(diagnose_route, "GEMINI_DIAGNOSE_KEY", "configured"),
            patch.object(diagnose_route, "GROQ_API_KEY", "configured"),
            patch.object(diagnose_route, "GEMMA_DIAGNOSE_KEY", ""),
            patch.object(diagnose_route, "ai_is_crop_image", return_value=(True, None)),
            patch.object(diagnose_route, "_run_gemini_pass", return_value=(None, None)) as gemini,
            patch.object(diagnose_route, "_run_vision_pass", return_value=None) as groq,
            patch.object(diagnose_route, "_run_gemma_pass") as gemma,
            patch.object(
                diagnose_route,
                "predict_offline_diagnosis",
                return_value=offline_result.copy(),
            ) as offline,
            patch.object(diagnose_route, "_save_diagnosis_image", return_value="image.jpg"),
            patch.object(diagnose_route, "_log_diagnosis"),
        ):
            response = app.test_client().post(
                "/api/diagnose",
                json={"image": "dGVzdA==", "lang": "en"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["offline_fallback"])
        self.assertTrue(any(
            "[Diagnosis result] Answered by offline:plantvillage-15-class"
            " | disease=Tomato Early Blight | confidence=90%" in message
            for message in captured.output
        ))
        gemini.assert_called_once()
        groq.assert_called_once()
        gemma.assert_not_called()
        offline.assert_called_once_with("dGVzdA==")

    def test_gemma_is_tried_after_hosted_providers_before_offline_fallback(self):
        app = Flask(__name__)
        app.register_blueprint(diagnose_bp)
        gemma_result = {"disease": "Tomato Early Blight", "confidence": 88}
        with (
            patch.object(diagnose_route, "GEMINI_DIAGNOSE_KEY", ""),
            patch.object(diagnose_route, "GROQ_API_KEY", ""),
            patch.object(diagnose_route, "GEMMA_DIAGNOSE_KEY", "configured"),
            patch.object(diagnose_route, "ai_is_crop_image", return_value=(True, None)),
            patch.object(diagnose_route, "_run_gemini_pass") as gemini,
            patch.object(diagnose_route, "_run_vision_pass") as groq,
            patch.object(diagnose_route, "_run_gemma_pass", return_value=gemma_result) as gemma,
            patch.object(diagnose_route, "predict_offline_diagnosis") as offline,
            patch.object(diagnose_route, "_save_diagnosis_image", return_value="image.jpg"),
            patch.object(diagnose_route, "_log_diagnosis"),
        ):
            response = app.test_client().post(
                "/api/diagnose",
                json={"image": "dGVzdA==", "lang": "en"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["disease"], "Tomato Early Blight")
        self.assertEqual(response.get_json()["_models_used"], ["gemma:gemma-3-27b-it"])
        gemini.assert_not_called()
        groq.assert_not_called()
        gemma.assert_called_once()
        offline.assert_not_called()

    def test_successful_gemini_result_does_not_call_groq_or_offline_model(self):
        app = Flask(__name__)
        app.register_blueprint(diagnose_bp)
        gemini_result = {"disease": "Tomato Early Blight", "confidence": 90}
        with (
            self.assertLogs("smartagro", level="INFO") as captured,
            patch.object(diagnose_route, "GEMINI_DIAGNOSE_KEY", "configured"),
            patch.object(diagnose_route, "GROQ_API_KEY", "configured"),
            patch.object(diagnose_route, "GEMMA_DIAGNOSE_KEY", "configured"),
            patch.object(diagnose_route, "ai_is_crop_image", return_value=(True, None)),
            patch.object(
                diagnose_route,
                "_run_gemini_pass",
                return_value=(gemini_result, "test-model"),
            ),
            patch.object(diagnose_route, "_run_vision_pass") as groq,
            patch.object(diagnose_route, "_run_gemma_pass") as gemma,
            patch.object(diagnose_route, "predict_offline_diagnosis") as offline,
            patch.object(diagnose_route, "_save_diagnosis_image", return_value="image.jpg"),
            patch.object(diagnose_route, "_log_diagnosis"),
        ):
            response = app.test_client().post(
                "/api/diagnose",
                json={"image": "dGVzdA==", "lang": "en"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["disease"], "Tomato Early Blight")
        self.assertNotIn("offline_fallback", response.get_json())
        messages = "\n".join(captured.output)
        gemini_step = messages.index("[Diagnosis step 2/5] Gemini diagnosis: trying")
        groq_step = messages.index("[Diagnosis step 3/5] Groq diagnosis: skipped")
        gemma_step = messages.index("[Diagnosis step 4/5] Gemma diagnosis: skipped")
        offline_step = messages.index("[Diagnosis step 5/5] Offline model: skipped")
        self.assertLess(gemini_step, groq_step)
        self.assertLess(groq_step, gemma_step)
        self.assertLess(gemma_step, offline_step)
        self.assertTrue(any(
            "[Diagnosis result] Answered by gemini:test-model"
            " | disease=Tomato Early Blight | confidence=90%" in message
            for message in captured.output
        ))
        groq.assert_not_called()
        gemma.assert_not_called()
        offline.assert_not_called()

    def test_offline_fallback_works_when_no_api_keys_are_configured(self):
        app = Flask(__name__)
        app.register_blueprint(diagnose_bp)
        offline_result = {
            "disease": "Tomato Early Blight",
            "confidence": 90,
            "offline_fallback": True,
            "offline_notice": "Offline classifier notice.",
            "_english_source": {"disease": "Tomato Early Blight"},
        }
        with (
            patch.object(diagnose_route, "GEMINI_DIAGNOSE_KEY", ""),
            patch.object(diagnose_route, "GROQ_API_KEY", ""),
            patch.object(diagnose_route, "GEMMA_DIAGNOSE_KEY", ""),
            patch.object(diagnose_route, "ai_is_crop_image", return_value=(True, None)),
            patch.object(diagnose_route, "_run_gemini_pass") as gemini,
            patch.object(diagnose_route, "_run_vision_pass") as groq,
            patch.object(diagnose_route, "_run_gemma_pass") as gemma,
            patch.object(
                diagnose_route,
                "predict_offline_diagnosis",
                return_value=offline_result,
            ) as offline,
            patch.object(diagnose_route, "_save_diagnosis_image", return_value="image.jpg"),
            patch.object(diagnose_route, "_log_diagnosis"),
        ):
            response = app.test_client().post(
                "/api/diagnose",
                json={"image": "dGVzdA==", "lang": "en"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["offline_fallback"])
        gemini.assert_not_called()
        groq.assert_not_called()
        gemma.assert_not_called()
        offline.assert_called_once_with("dGVzdA==")

    def test_missing_offline_model_returns_service_unavailable_after_api_failures(self):
        app = Flask(__name__)
        app.register_blueprint(diagnose_bp)
        with (
            patch.object(diagnose_route, "GEMINI_DIAGNOSE_KEY", ""),
            patch.object(diagnose_route, "GROQ_API_KEY", ""),
            patch.object(diagnose_route, "GEMMA_DIAGNOSE_KEY", ""),
            patch.object(diagnose_route, "ai_is_crop_image", return_value=(True, None)),
            patch.object(
                diagnose_route,
                "predict_offline_diagnosis",
                side_effect=offline_diagnosis.OfflineDiagnosisUnavailable("model missing"),
            ),
        ):
            response = app.test_client().post(
                "/api/diagnose",
                json={"image": "dGVzdA==", "lang": "en"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("local fallback", response.get_json()["error"])

    def test_accuracy_endpoint_does_not_claim_unmeasured_accuracy(self):
        app = Flask(__name__)
        app.register_blueprint(diagnose_bp)
        with patch.object(diagnose_route, "DEBUG_MODE", True):
            response = app.test_client().get("/api/diagnose-log/accuracy")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["accuracy"])
        self.assertEqual(response.get_json()["status"], "not_measured")


if __name__ == "__main__":
    unittest.main()
