from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.external_footprint import validate_observation

from norway_company_agent.footprint import (
    extract_contact_and_location_signals,
    extract_website_footprint_observations,
    research_external_footprint,
    research_local_reviews_and_presence,
)


class TestFootprintResearch(unittest.TestCase):
    def test_extract_website_footprint_observations(self):
        profile = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "http://www.aelektronikk.no/",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "content_sha256": "4d941ddb7c5ee90222cd93099dd9d816268069fa99d9e7b09e17aba1e48bf2ca",
                    "value": {
                        "final_url": "http://www.aelektronikk.no/",
                        "title": "Aas Elektronikk AS",
                        "description": "Engineering services in Norway",
                        "content_sha256": "4d941ddb7c5ee90222cd93099dd9d816268069fa99d9e7b09e17aba1e48bf2ca",
                        "identity_assessment": {
                            "publishable": True,
                            "status": "exact",
                            "score": 0.95,
                        },
                        "pages": [
                            {"url": "http://www.aelektronikk.no/", "title": "Home", "content_sha256": "a" * 64},
                            {"url": "http://www.aelektronikk.no/nyheter", "title": "Siste Nyheter", "content_sha256": "b" * 64},
                        ],
                        "social_links": [
                            {"platform": "linkedin", "url": "https://linkedin.com/company/aas-elektronikk"},
                        ],
                    },
                },
            },
        }

        accepted, rejected = extract_website_footprint_observations(profile)
        self.assertEqual(len(rejected), 0)
        self.assertGreaterEqual(len(accepted), 2)  # Site activity + news + social
        for obs in accepted:
            errs = validate_observation(obs)
            self.assertEqual(errs, [], f"Observation {obs['id']} failed validation: {errs}")

    def test_extract_website_footprint_quarantined_rejection(self):
        profile = {
            "organisation_number": "123456789",
            "name": "SUSPICIOUS COMPANY AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "value": {
                        "identity_assessment": {
                            "publishable": False,
                            "status": "related_or_uncertain",
                            "score": 0.4,
                        },
                    },
                },
            },
        }
        accepted, rejected = extract_website_footprint_observations(profile)
        self.assertEqual(len(accepted), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("quarantined", rejected[0]["reasons"][0])

    def test_contact_and_location_signals(self):
        profile = {
            "organisation_number": "916340257",
            "name": "WYSSEN NORGE AS",
            "business_address": "Sentrumsvegen 1",
            "postal_code": "6856",
            "postal_area": "SOGNDAL",
            "municipality": "SOGNDAL",
            "evidence": {
                "registry": {
                    "value": {
                        "forretningsadresse.adresse": "Sentrumsvegen 1",
                        "forretningsadresse.postnummer": "6856",
                        "forretningsadresse.poststed": "SOGNDAL",
                        "forretningsadresse.kommune": "SOGNDAL",
                        "telefon": "57 67 00 00",
                    },
                },
                "locations": {
                    "value": {
                        "locations": [
                            {"name": "WYSSEN NORGE AS AVD SOGNDAL", "organisasjonsnummer": "999111222"},
                            {"name": "WYSSEN NORGE AS AVD TROMSØ", "organisasjonsnummer": "999111333"},
                        ]
                    }
                },
                "website": {
                    "value": {
                        "identity_assessment": {"publishable": True},
                        "final_url": "https://wyssen.no/",
                        "pages": [{"url": "https://wyssen.no/kontakt"}],
                    }
                }
            },
        }

        contact = extract_contact_and_location_signals(profile)
        self.assertEqual(contact["business_address"]["city"], "SOGNDAL")
        self.assertEqual(contact["phone"], "57 67 00 00")
        self.assertEqual(contact["registered_workplaces_count"], 2)
        self.assertEqual(contact["website_contact"]["contact_page"], "https://wyssen.no/kontakt")

    def test_research_external_footprint_full(self):
        profile = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "http://www.aelektronikk.no/",
                    "retrieved_at": "2026-10-02T10:00:00Z",
                    "content_sha256": "4d941ddb7c5ee90222cd93099dd9d816268069fa99d9e7b09e17aba1e48bf2ca",
                    "value": {
                        "final_url": "http://www.aelektronikk.no/",
                        "title": "Aas Elektronikk AS",
                        "description": "Mekatronikk og engineering",
                        "identity_assessment": {"publishable": True, "status": "exact", "score": 0.95},
                        "pages": [{"url": "http://www.aelektronikk.no/", "title": "Home", "content_sha256": "a" * 64}],
                        "social_links": [],
                    },
                },
                "registry": {"value": {}},
                "locations": {"value": {"locations": []}},
            },
        }

        ev, metrics = research_external_footprint(profile)
        self.assertEqual(ev["field"], "external_footprint")
        self.assertEqual(ev["status"], "available")
        self.assertGreaterEqual(ev["value"]["accepted_observations"], 1)
        self.assertEqual(ev["value"]["sentiment"]["status"], "abstain")
