from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from generator import canonical


class CanonicalSourceTests(unittest.TestCase):
    def test_loads_profile_and_products_into_vendor_content_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vendor_dir = root / "example-vendor"
            vendor_dir.mkdir()
            (vendor_dir / "profile.json").write_text(
                json.dumps(
                    {
                        "target_id": "example-vendor",
                        "display_name": "Example Vendor",
                        "homepage_url": "https://example.com",
                        "products_categories": {
                            "items": [
                                {"category": "sensors_and_navigation", "is_primary": True},
                                {"category": "communications", "is_primary": False},
                            ]
                        },
                        "headquarters": {"value": {"city": "Austin", "state_or_province": "Texas", "country": "United States"}},
                        "drone_supply_chain_role": {"value": "component_supplier"},
                        "ndaa": {"value": "yes"},
                        "blue_uas": {"value": None},
                        "readiness": {"value": "production"},
                        "tagline": "Makes test components for unmanned systems.",
                        "meta": {"created_at": "2026-05-02T12:00:00+00:00"},
                    }
                )
            )
            (vendor_dir / "products.json").write_text(
                json.dumps(
                    {
                        "products": [
                            {
                                "name": "Example INS",
                                "category": "sensors_and_navigation",
                                "descriptor": "Compact inertial navigation unit",
                                "readiness": "production",
                                "ndaa": "yes",
                                "blue_uas": "unknown",
                                "evidence": [{"url": "https://example.com/ins", "snippet": "INS product"}],
                            }
                        ]
                    }
                )
            )
            (vendor_dir / "canonicalize_report.json").write_text(json.dumps({"ts": "2026-05-03T20:22:16+00:00"}))

            vendors = canonical.load_vendors(root, {})

        self.assertEqual(len(vendors), 1)
        vendor = vendors[0]
        self.assertEqual(vendor["slug"], "example-vendor")
        self.assertEqual(vendor["primary_category"], "sensors-navigation")
        self.assertEqual(vendor["categories"], ["sensors-navigation", "communications"])
        self.assertEqual(vendor["compliance_posture"], "ndaa_compliant")
        self.assertEqual(vendor["readiness_posture"], "production")
        self.assertEqual(vendor["reviewed_at"], "2026-05-03")
        self.assertEqual(vendor["products"][0]["source_url"], "https://example.com/ins")


if __name__ == "__main__":
    unittest.main()
