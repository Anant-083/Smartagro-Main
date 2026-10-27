import unittest
from unittest.mock import Mock, patch

from app import app
from routes import alerts, chat, diagnose, market, weather


class ApplicationSmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_primary_pages_render_successfully(self):
        with patch("app.track_usage"):
            for path in ("/", "/diagnose", "/market", "/alerts", "/offline", "/usage"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)

    def test_health_readiness_and_service_worker_routes(self):
        with patch("app.track_usage"):
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            readiness = self.client.get("/readyz")
            service_worker = self.client.get("/service-worker.js")

        self.assertEqual(readiness.status_code, 200)
        self.assertIn("gemma_diagnose", readiness.get_json())
        self.assertEqual(service_worker.status_code, 200)
        self.assertIn("javascript", service_worker.content_type)
        service_worker.close()

    def test_missing_required_inputs_return_clear_client_errors(self):
        cases = (
            ("get", "/api/weather"),
            ("get", "/api/vegetation"),
            ("post", "/api/diagnose", {}),
            ("post", "/api/alerts-forecast", {}),
            ("post", "/api/crop-risk", {}),
        )
        with patch("app.track_usage"):
            for method, path, *body in cases:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(
                        path,
                        json=body[0] if body else None,
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("error", response.get_json())

    def test_alert_forecast_and_crop_risk_routes_return_structured_results(self):
        forecast = [
            {
                "date": "2026-10-28",
                "temp_max": 32,
                "temp_min": 24,
                "humidity": 70,
                "wind_speed": 5,
                "rain": 0,
            }
        ]
        with (
            patch("app.track_usage"),
            patch.object(alerts, "_ai_alerts_for_forecast", return_value=None),
        ):
            forecast_response = self.client.post(
                "/api/alerts-forecast",
                json={"forecast": forecast},
            )
            risk_response = self.client.post(
                "/api/crop-risk",
                json={"crops": [{"name": "Rice"}], "forecast": forecast},
            )

        self.assertEqual(forecast_response.status_code, 200)
        self.assertEqual(len(forecast_response.get_json()["daily"]), 1)
        self.assertEqual(risk_response.status_code, 200)
        self.assertEqual(risk_response.get_json()["crops"][0]["name"], "Rice")

    def test_weather_route_combines_mocked_current_and_forecast_data(self):
        current_response = Mock(status_code=200, text="")
        current_response.json.return_value = {
            "name": "Pune",
            "main": {
                "temp": 25.4, "feels_like": 26.1, "humidity": 60, "pressure": 1010,
            },
            "weather": [{"description": "clear sky", "icon": "01d"}],
            "wind": {"speed": 2.5},
            "visibility": 10000,
        }
        forecast_response = Mock(status_code=200, text="")
        forecast_response.json.return_value = {
            "list": [{
                "dt": 1793145600,
                "main": {"temp_max": 29, "temp_min": 20, "humidity": 65},
                "weather": [{"description": "clear sky", "icon": "01d"}],
                "wind": {"speed": 3},
            }]
        }

        def weather_response(url, timeout):
            return forecast_response if "/forecast" in url else current_response

        with (
            patch("app.track_usage"),
            patch.object(weather.requests, "get", side_effect=weather_response),
            patch.object(weather, "_fetch_extended_forecast", return_value=[]),
        ):
            response = self.client.get("/api/weather?lat=18.52&lon=73.85")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["current"]["city"], "Pune")
        self.assertEqual(body["current"]["temp"], 25)
        self.assertEqual(len(body["forecast"]), 1)

    def test_crop_recommendations_identify_rule_based_source(self):
        with (
            patch("app.track_usage"),
            patch.object(weather, "ai_recommend_crops", return_value=None),
        ):
            response = self.client.post(
                "/api/crop-recommendations",
                json={"temp": 25, "humidity": 70, "city": "Pune"},
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["source"], "rule_based")
        self.assertEqual(len(result["crops"]), 6)
        self.assertTrue(result["calendar"])

    def test_market_route_marks_mocked_prices_as_live(self):
        prices = [{"crop": "Potato", "price": 2000, "change": 2.5}]
        with (
            patch("app.track_usage"),
            patch.object(market, "fetch_agmarknet_prices", return_value=prices),
        ):
            response = self.client.get("/api/market?location=Delhi")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["live_count"], 1)
        self.assertEqual(body["city_meta"]["Delhi"]["data_status"], "live")
        self.assertEqual(body["markets"]["Delhi"][0]["crop"], "Potato")

    def test_chat_prompt_uses_selected_language_for_english_input(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": "• Use an approved crop protection method."}}]
        }
        with (
            patch("app.track_usage"),
            patch.object(chat, "GROQ_API_KEY", "configured"),
            patch.object(chat.requests, "post", return_value=response) as post,
        ):
            result = self.client.post(
                "/api/chat",
                json={
                    "lang": "hi",
                    "messages": [{
                        "role": "user",
                        "content": "How can I protect my wheat from aphids?",
                    }],
                },
            )

        self.assertEqual(result.status_code, 200)
        system_prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("selected language (Hindi)", system_prompt)
        self.assertIn("most recent user message is in English", system_prompt)

    def test_unknown_market_location_is_reported_without_external_fetch(self):
        with patch("app.track_usage"):
            response = self.client.get("/api/market?location=not-a-supported-city")

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["locations"], [])
        self.assertEqual(result["markets"], {})
        self.assertIn("No market mapping", result["note"])

    def test_debug_routes_are_disabled_outside_debug_mode(self):
        with (
            patch("app.track_usage"),
            patch.object(weather, "DEBUG_MODE", False),
            patch.object(market, "DEBUG_MODE", False),
            patch.object(diagnose, "DEBUG_MODE", False),
        ):
            responses = (
                self.client.get("/api/debug-extended-forecast"),
                self.client.get("/api/debug-market"),
                self.client.get("/api/diagnose-log"),
                self.client.get("/api/diagnose-log/accuracy"),
            )

        self.assertEqual([response.status_code for response in responses], [403] * 4)


if __name__ == "__main__":
    unittest.main()
