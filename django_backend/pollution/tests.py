from django.test import TestCase
from django.urls import reverse

from .views import calculate_sub_index, calculate_cpcb_aqi, get_level


class AQICalculationTests(TestCase):
    def test_aqi_category_boundaries(self):
        self.assertEqual(get_level(50), "Good")
        self.assertEqual(get_level(100), "Satisfactory")
        self.assertEqual(get_level(200), "Moderate")
        self.assertEqual(get_level(300), "Poor")
        self.assertEqual(get_level(400), "Very Poor")
        self.assertEqual(get_level(401), "Severe")

    def test_sub_index_interpolates_breakpoint(self):
        result = calculate_sub_index(25, [(0, 50, 0, 50)])
        self.assertEqual(result, 25)

    def test_negative_or_invalid_concentration_is_rejected(self):
        self.assertIsNone(calculate_sub_index(-1, [(0, 50, 0, 50)]))
        self.assertIsNone(calculate_sub_index("invalid", [(0, 50, 0, 50)]))

    def test_overall_aqi_uses_highest_available_sub_index(self):
        aqi, indices = calculate_cpcb_aqi({
            "pm10": 25,
            "pm2_5": 60,
            "no2": None,
            "so2": None,
            "o3": None,
            "nh3": None,
            "co": None,
        })
        self.assertIsNotNone(aqi)
        self.assertEqual(aqi, round(max(indices.values())))

    def test_no_pollutant_data_returns_no_aqi(self):
        aqi, indices = calculate_cpcb_aqi({})
        self.assertIsNone(aqi)
        self.assertEqual(indices, {})


class PublicEndpointTests(TestCase):
    def test_homepage_returns_success(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_pollution_endpoint_requires_coordinates(self):
        response = self.client.get("/pollution/")
        self.assertEqual(response.status_code, 400)

    def test_pollution_endpoint_rejects_out_of_range_coordinates(self):
        response = self.client.get("/pollution/?lat=999&lon=77")
        self.assertEqual(response.status_code, 400)

    def test_prediction_endpoint_returns_json(self):
        response = self.client.get("/api/predict/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("prediction", response.json())
