from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from norway_company_agent.official import serve_snapshot_registry_live, snapshot_normalized_entity
from norway_company_agent.planner import (
    CompanyArchetype,
    classify_company_archetype,
    plan_company_research,
)
from norway_company_agent.evidence import evidence


class TestV10BulkBaseline(unittest.TestCase):
    def setUp(self):
        self.sample_profile = {
            "organisation_number": "985589003",
            "name": "ARKITEKTFIRMA JON VIKØREN AS",
            "legal_form": "AS",
            "employees": None,
            "bankrupt": False,
            "liquidating": False,
            "municipality": "VIK",
            "municipality_number": "4639",
            "industry_code": "71.110",
            "industry_label": "Arkitektvirksomhet",
            "website": "",
            "latest_submitted_accounts": "2025",
            "evidence": {
                "registry": evidence(
                    "registry",
                    "available",
                    "official_registry_bulk",
                    "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv",
                    value={
                        "organisasjonsnummer": "985589003",
                        "navn": "ARKITEKTFIRMA JON VIKØREN AS",
                        "organisasjonsform.kode": "AS",
                        "forretningsadresse.kommune": "VIK",
                        "forretningsadresse.postnummer": "6893",
                        "forretningsadresse.adresse": "Tomtebu 2",
                        "forretningsadresse.poststed": "VIK I SOGN",
                        "sisteInnsendteAarsregnskap": "2025",
                    },
                    retrieved_at="2026-10-02T12:00:00Z",
                    content_sha256="abc123hash",
                    source_row_key="985589003",
                )
            },
        }

    def test_snapshot_normalized_entity_extraction(self):
        norm = snapshot_normalized_entity(self.sample_profile)
        self.assertEqual(norm["organisation_number"], "985589003")
        self.assertEqual(norm["name"], "ARKITEKTFIRMA JON VIKØREN AS")
        self.assertEqual(norm["legal_form"], "AS")
        self.assertEqual(norm["business_address"]["kommune"], "VIK")
        self.assertEqual(norm["business_address"]["postnummer"], "6893")
        self.assertEqual(norm["latest_submitted_accounts"], "2025")

    def test_serve_snapshot_registry_live_provenance(self):
        ev = serve_snapshot_registry_live(self.sample_profile)
        self.assertEqual(ev["field"], "registry_live")
        self.assertEqual(ev["status"], "available")
        self.assertEqual(ev["source_type"], "statutory_registry_snapshot")
        self.assertEqual(ev["retrieved_at"], "2026-10-02T12:00:00Z")
        self.assertEqual(ev["content_sha256"], "abc123hash")
        self.assertIn("Tier 1", ev.get("note", ""))

    def test_planner_skips_redundant_registry_live_with_bulk_baseline(self):
        plan = plan_company_research(self.sample_profile, use_bulk_baseline=True)
        self.assertFalse(plan.should_execute("registry_live"))
        self.assertIn("registry_live", plan.paths_skipped)
        self.assertIn("fully satisfied by local statutory registry snapshot", plan.skip_reasons["registry_live"])
        self.assertGreater(plan.estimated_requests_saved, 0)

    def test_planner_preserves_external_research_for_commercial_operating(self):
        comm_profile = dict(self.sample_profile)
        comm_profile["website"] = "https://www.arkitekt-vikoren.no"
        comm_profile["employees"] = 4
        # Architecture (71.110): preserves website and financials; reviews deferred as non-craftsman
        plan = plan_company_research(comm_profile, use_bulk_baseline=True)
        self.assertEqual(plan.archetype, CompanyArchetype.COMMERCIAL_OPERATING)
        self.assertTrue(plan.should_execute("website_crawl"))
        self.assertTrue(plan.should_execute("financials"))
        self.assertFalse(plan.should_execute("workforce_external_jobs"))
        self.assertIn("expected yield", plan.skip_reasons["workforce_external_jobs"])
        self.assertFalse(plan.should_execute("reviews_local_presence"))
        self.assertIn("Fagfolkguiden", plan.skip_reasons["reviews_local_presence"])

        # Craftsman/trade company (43.210 Electrical): preserves reviews_local_presence
        trade_profile = dict(comm_profile, industry_code="43.210", industry_label="Elektrisk installasjonsarbeid")
        plan_trade = plan_company_research(trade_profile, use_bulk_baseline=True)
        self.assertTrue(plan_trade.should_execute("reviews_local_presence"))

    def test_planner_housing_skips_website_crawl(self):
        housing_profile = dict(self.sample_profile)
        housing_profile["legal_form"] = "BRL"
        housing_profile["name"] = "VIK BORETTSLAG"
        housing_profile["website"] = "https://www.obos.no"
        plan = plan_company_research(housing_profile, use_bulk_baseline=True)
        self.assertFalse(plan.should_execute("website_crawl"))
        self.assertIn("property management", plan.skip_reasons["website_crawl"])

    def test_planner_defers_external_noise_for_dormant_and_housing(self):
        housing_profile = dict(self.sample_profile)
        housing_profile["legal_form"] = "BRL"
        housing_profile["name"] = "VIK BORETTSLAG"
        housing_profile["industry_code"] = "97.001"
        housing_profile["latest_submitted_accounts"] = None
        housing_profile["evidence"] = {
            "registry": {
                "value": {
                    "organisasjonsnummer": "985589003",
                    "navn": "VIK BORETTSLAG",
                    "organisasjonsform.kode": "BRL",
                }
            }
        }
        plan = plan_company_research(housing_profile, use_bulk_baseline=True)
        self.assertEqual(plan.archetype, CompanyArchetype.RESIDENTIAL_HOUSING)
        self.assertFalse(plan.should_execute("workforce_external_jobs"))
        self.assertFalse(plan.should_execute("reviews_local_presence"))
        self.assertFalse(plan.should_execute("financials"))
        self.assertFalse(plan.should_execute("group"))

        # Housing with submitted accounts should execute financials
        housing_with_accounts = dict(housing_profile, latest_submitted_accounts="2025")
        plan_with_accounts = plan_company_research(housing_with_accounts, use_bulk_baseline=True)
        self.assertTrue(plan_with_accounts.should_execute("financials"))

    def test_housing_financials_routed_only_when_accounts_present(self):
        # ESEK entity with latest_submitted_accounts
        esek_with_accounts = {
            "organisation_number": "925800023",
            "name": "SAMEIET LENSMANNSTUNET 1",
            "legal_form": "ESEK",
            "industry_code": "97.001",
            "latest_submitted_accounts": "2025",
        }
        plan_esek = plan_company_research(esek_with_accounts, use_bulk_baseline=True)
        self.assertTrue(plan_esek.should_execute("financials"))

        # ESEK entity without accounts
        esek_no_accounts = {
            "organisation_number": "925800023",
            "name": "SAMEIET LENSMANNSTUNET 1",
            "legal_form": "ESEK",
            "industry_code": "97.001",
            "latest_submitted_accounts": None,
        }
        plan_esek_none = plan_company_research(esek_no_accounts, use_bulk_baseline=True)
        self.assertFalse(plan_esek_none.should_execute("financials"))

    def test_fetch_website_skips_secondary_crawl_when_identity_fails(self):
        import io
        from unittest.mock import patch, MagicMock
        from norway_company_agent.website import fetch_website

        # Mismatched company name (e.g. Butikkdrift vs 7-Eleven)
        profile = {
            "organisation_number": "935354293",
            "name": "BUTIKKDRIFT UMAZABAL GUERRA AS",
            "website": "www.testfranchise.no",
        }

        class MockResponse:
            def __init__(self, content, url):
                self.raw = io.BytesIO(content)
                self.url = url
                self.headers = {"content-type": "text/html; charset=utf-8"}
            def read(self, n=-1):
                return self.raw.read(n)
            def geturl(self):
                return self.url
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        opened_urls = []
        def fake_open(req, timeout=15.0):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            opened_urls.append(url)
            if "robots.txt" in url:
                return MockResponse(b"User-agent: *\nAllow: /\n", url)
            return MockResponse(
                b"<html><head><title>Franchise Brand HQ</title></head><body>Welcome to Franchise Brand HQ!<a href='/about'>About</a><a href='/contact'>Contact</a></body></html>",
                url,
            )

        with patch("norway_company_agent.website.assert_public_url", lambda u: None), \
             patch("norway_company_agent.website.SAFE_OPENER.open", side_effect=fake_open):
            record, metrics = fetch_website("https://www.testfranchise.no", profile=profile, max_secondary_pages=1)
            pages = record.get("value", {}).get("pages", [])
            # Must ONLY contain homepage (1 page) because franchise HQ fails exact legal entity match
            self.assertEqual(len(pages), 1)
            # Secondary pages should NOT have been fetched
            self.assertNotIn("https://www.testfranchise.no/about", opened_urls)
            self.assertNotIn("https://www.testfranchise.no/contact", opened_urls)

    def test_fetch_website_crawls_secondary_page_when_identity_matches(self):
        import io
        from unittest.mock import patch, MagicMock
        from norway_company_agent.website import fetch_website

        # Matching company name (2 core tokens)
        profile = {
            "organisation_number": "888567232",
            "name": "AAS ELEKTRONIKK AS",
            "website": "www.aelektronikk.no",
        }

        class MockResponse:
            def __init__(self, content, url):
                self.raw = io.BytesIO(content)
                self.url = url
                self.headers = {"content-type": "text/html; charset=utf-8"}
            def read(self, n=-1):
                return self.raw.read(n)
            def geturl(self):
                return self.url
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        opened_urls = []
        def fake_open(req, timeout=15.0):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            opened_urls.append(url)
            if "robots.txt" in url:
                return MockResponse(b"User-agent: *\nAllow: /\n", url)
            if "/om-oss" in url:
                return MockResponse(
                    b"<html><head><title>Om oss | Aas Elektronikk AS</title></head><body>Om oss i Aas Elektronikk AS</body></html>",
                    url,
                )
            return MockResponse(
                b"<html><head><title>Aas Elektronikk AS</title></head><body>Velkommen til Aas Elektronikk AS, vi leverer hoykvalitets elektroniske komponenter og tjenester for industrien over hele landet.<a href='/om-oss'>Om oss</a><a href='/kontakt'>Kontakt</a></body></html>",
                url,
            )

        with patch("norway_company_agent.website.assert_public_url", lambda u: None), \
             patch("norway_company_agent.website.SAFE_OPENER.open", side_effect=fake_open):
            record, metrics = fetch_website("https://www.aelektronikk.no", profile=profile, max_secondary_pages=1)
            pages = record.get("value", {}).get("pages", [])
            # Exactly 2 pages (homepage + 1 priority secondary page)
            self.assertEqual(len(pages), 2)
            self.assertIn("https://www.aelektronikk.no/om-oss", opened_urls)
            # Budget was capped at max 1 secondary page, so kontakt was not fetched
            self.assertNotIn("https://www.aelektronikk.no/kontakt", opened_urls)
            # Total requests: 1 robots + 1 homepage + 1 secondary page = 3 requests
            self.assertEqual(metrics["requests"], 3)

