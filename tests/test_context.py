from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant import context_tools


class ContextTests(unittest.TestCase):
    def test_manual_city_does_not_request_network(self):
        with patch.object(context_tools, "LOCAL_CONFIG", {"location": "Новосибирск"}), patch.object(context_tools, "urlopen") as request:
            self.assertEqual(context_tools.get_location()["city"], "Новосибирск")
            request.assert_not_called()

    def test_ip_location_explicitly_approximate_and_cached(self):
        with patch.object(context_tools, "LOCAL_CONFIG", {}), patch.object(context_tools, "_location_cache", None), patch.object(context_tools, "urlopen") as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({"success": True, "city": "Example", "latitude": 50, "longitude": 40, "ip": "not returned"}).encode()
            result = context_tools.get_location()
            self.assertTrue(result["approximate"])
            self.assertNotIn("ip", result)
            self.assertEqual(context_tools.get_location(), result)
            self.assertEqual(request.call_count, 1)

    def test_location_failure_does_not_guess_city(self):
        with patch.object(context_tools, "LOCAL_CONFIG", {}), patch.object(context_tools, "_location_cache", None), patch.object(context_tools, "urlopen", side_effect=TimeoutError("offline")):
            result = context_tools.get_location()
            self.assertIn("error", result)
            self.assertNotIn("city", result)


if __name__ == "__main__":
    unittest.main()
