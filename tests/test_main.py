import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import main
from app.errors import UnreliableGeometryError
from app.models import GeometryRequest


class GeometryResponseCorrelationTests(unittest.TestCase):
    request_id = "roof-run-123"

    def _request(self):
        return GeometryRequest.model_construct(requestId=self.request_id)

    def _settings(self):
        return SimpleNamespace(max_concurrent_jobs=1)

    def test_geometry_rejection_returns_request_id_with_failure_details(self):
        error = UnreliableGeometryError(
            "NO_RELIABLE_LIDAR_GEOMETRY",
            "No registered LiDAR candidate produced reliable roof geometry for this property.",
            details={"selectionAttempts": [{"errorCode": "EDGE_NODING_FAILED"}]},
        )
        with (
            patch.object(main, "job_semaphore", return_value=asyncio.Semaphore(1)),
            patch.object(main, "reconstruct_roof", side_effect=error),
        ):
            response = asyncio.run(main.roof_geometry(self._request(), self._settings()))

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.headers["x-request-id"], self.request_id)
        payload = json.loads(response.body)
        self.assertEqual(payload["requestId"], self.request_id)
        self.assertEqual(
            payload["details"]["selectionAttempts"][0]["errorCode"],
            "EDGE_NODING_FAILED",
        )

    def test_success_response_returns_request_id_header(self):
        result = {
            "verificationStatus": "VERIFIED",
            "pricingAllowed": True,
            "geometry": {"confidence": 0.95},
        }
        with (
            patch.object(main, "job_semaphore", return_value=asyncio.Semaphore(1)),
            patch.object(main, "reconstruct_roof", return_value=result),
        ):
            response = asyncio.run(main.roof_geometry(self._request(), self._settings()))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-request-id"], self.request_id)


if __name__ == "__main__":
    unittest.main()
