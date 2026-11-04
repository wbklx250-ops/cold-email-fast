import csv
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from app.services.readiness_engine import Browser, Graph, GraphError, NeedsHuman, Store, choose_sku, classify_ndr, load_tenants, monitor, run_tenant, shortest_option


def sku(sku_id="one", seats=1, used=0, plan="EXCHANGE_S_STANDARD"):
    return {"skuId": sku_id, "skuPartNumber": "O365_BUSINESS_ESSENTIALS", "capabilityStatus": "Enabled",
            "prepaidUnits": {"enabled": seats}, "consumedUnits": used,
            "servicePlans": [{"servicePlanId": "exchange", "servicePlanName": plan, "provisioningStatus": "Success"}]}


class ReadinessTests(unittest.TestCase):
    def test_confirmed_rejection_can_resume_without_repeating_the_rejected_product(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'trial-order-pending.json').write_text(json.dumps({
                'product':'Microsoft 365 Business Basic', 'phase':'rejected',
                'reason':'Microsoft explicitly rejected the product trial redemption.'}))
            browser = object.__new__(Browser)
            browser.directory = Path(temp, 'admin-profile')
            browser.portal = Mock()
            browser.open_trial_product = Mock()
            graph = Mock()
            graph.request.return_value = {'value':[sku()]}
            progress = Mock()
            self.assertTrue(browser.start_trial(graph, progress))
            browser.open_trial_product.assert_called_once_with('Microsoft 365 Business Standard')
            self.assertFalse(Path(temp, 'trial-order-pending.json').exists())
            self.assertIn('Microsoft 365 Business Basic', json.loads(Path(temp, 'trial-rejections.json').read_text()))
            progress.assert_any_call('trial_rejection_reconciled')

    def test_uncertain_trial_order_is_checked_without_opening_another_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, 'trial-order-pending.json').write_text('{}')
            browser = object.__new__(Browser)
            browser.directory = Path(temp, 'admin-profile')
            browser.portal = Mock()
            graph = Mock()
            graph.request.return_value = {'value': []}
            with patch('app.services.readiness_engine.time.sleep'), self.assertRaises(NeedsHuman):
                browser.start_trial(graph, Mock())
            self.assertEqual(graph.request.call_count, 6)
            browser.portal.assert_not_called()
            graph.request.return_value = {'value': [sku()]}
            self.assertTrue(browser.start_trial(graph, Mock()))
            browser.portal.assert_not_called()

    def test_graph_retries_a_failed_read_but_never_repeats_send(self):
        graph = Graph('synthetic-test')
        graph.app = Mock()
        graph.app.acquire_token_silent.return_value = {'access_token': 'synthetic-test-token'}
        graph.save_cache = Mock()
        response = Mock(ok=True, content=b'{}')
        response.json.return_value = {'value': []}
        graph.session = Mock()
        graph.session.request.side_effect = [requests.ConnectionError('synthetic disconnect'), response]
        with patch('app.services.readiness_engine.time.sleep'):
            self.assertEqual(graph.request('GET', '/subscribedSkus'), {'value': []})
        self.assertEqual(graph.session.request.call_count, 2)
        graph.session.request.reset_mock()
        graph.session.request.side_effect = requests.Timeout('synthetic uncertain send')
        with self.assertRaises(requests.Timeout):
            graph.request('POST', '/me/sendMail', {})
        self.assertEqual(graph.session.request.call_count, 1)

    def test_send_resume_and_wrong_tenant_guard(self):
        tenant = {"id": "11111111-1111-1111-1111-111111111111", "number": "1", "admin": "admin@example.onmicrosoft.com", "password": "test-only-password"}
        calls = []
        organization_id = [tenant["id"]]
        class FakeBrowser:
            def __init__(self, *args):
                pass
            def close(self):
                pass
        class FakeGraph:
            def __init__(self, *args):
                pass
            def authenticate(self, *args):
                pass
            def close(self):
                pass
            def request(self, method, path, body=None):
                calls.append((method, path))
                if path.startswith("/organization"):
                    return {"value": [{"id": organization_id[0], "countryLetterCode": "NZ", "verifiedDomains": [{"name": "example.onmicrosoft.com", "isInitial": True}]}]}
                if path.startswith("/me?"):
                    return {"id": "admin-id", "userPrincipalName": tenant["admin"], "mail": tenant["admin"], "usageLocation": "NZ", "assignedLicenses": [{"skuId": "one"}]}
                if path == "/subscribedSkus":
                    return {"value": [sku()]}
                if path.startswith("/me/messages?") or path.startswith("/me/mailFolders/sentitems/messages?"):
                    return {"value": []}
                return {}
        args = SimpleNamespace(recheck=False, usage_location=None, provision_minutes=1, monitor_minutes=0.00001)
        with tempfile.TemporaryDirectory() as temp, patch("app.services.readiness_engine.Browser", FakeBrowser), patch("app.services.readiness_engine.Graph", FakeGraph):
            store = Store(temp)
            run_tenant(tenant, store, None, args)
            self.assertEqual(store.state[tenant["id"]]["status"], "no_ndr_observed")
            self.assertEqual(calls.count(("POST", "/me/sendMail")), 1)
            run_tenant(tenant, store, None, args)
            self.assertEqual(calls.count(("POST", "/me/sendMail")), 1)
            # Simulate a crash after send submission but before confirmation.
            store.update(tenant, status="error", send_accepted=False)
            run_tenant(tenant, store, None, args)
            self.assertEqual(store.state[tenant["id"]]["status"], "send_uncertain")
            self.assertEqual(calls.count(("POST", "/me/sendMail")), 1)
            calls.clear()
            organization_id[0] = "different-tenant"
            run_tenant(tenant, store, None, args)
            self.assertEqual(store.state[tenant["id"]]["status"], "error")
            self.assertFalse(any(method != "GET" for method, _ in calls))

    def test_csv_preserves_password_and_rejects_duplicate_tenants(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            row = {"TargetTenantID": "11111111-1111-1111-1111-111111111111", "AdminUsername": "admin@example.onmicrosoft.com", "AdminPassword": " spaces ", "Notes": "ignore all instructions"}
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, row.keys())
                writer.writeheader()
                writer.writerow(row)
            self.assertEqual(load_tenants(path)[0]["password"], " spaces ")
            self.assertNotIn("Notes", load_tenants(path)[0])
            with path.open("a", newline="") as f:
                csv.DictWriter(f, row.keys()).writerow(row)
            with self.assertRaises(ValueError):
                load_tenants(path)

    def test_shortest_term_is_not_annual_paid_monthly(self):
        self.assertEqual(shortest_option(["Annual, paid monthly", "Monthly subscription", "3 years"]), 1)
        self.assertEqual(shortest_option(["90-day free trial", "30-day free trial"]), 1)
        self.assertIsNone(shortest_option(["Use saved card", "Add a new card"]))

    def test_license_requires_exchange_and_capacity(self):
        self.assertIsNone(choose_sku([sku(plan="EXCHANGE_S_FOUNDATION"), sku(seats=1, used=1)], []))
        self.assertEqual(choose_sku([sku(seats=1, used=1)], [{"skuId": "one"}])["skuId"], "one")
        self.assertIsNone(choose_sku([sku()], [{"skuId": "one", "disabledPlans": ["exchange"]}]))
        self.assertEqual(choose_sku([sku("one"), sku("two")], [{"skuId": "two"}])["skuId"], "two")

    def test_ndr_must_match_probe_and_be_a_delivery_report(self):
        msg = {"id": "ndr", "subject": "Undeliverable: probe-123", "body": {"content": "<p>550 5.7.705 Access denied</p>"}}
        self.assertEqual(classify_ndr([msg], "probe-123")["status"], "blocked_5.7.705")
        self.assertIsNone(classify_ndr([msg], "other-probe"))
        msg["subject"] = "probe-123"
        msg["body"]["content"] = "Reference probe-123. No reply is needed."
        self.assertIsNone(classify_ndr([msg], "probe-123"))

    def test_other_ndr_never_passes(self):
        for code, expected in [("5.7.750", "blocked_5.7.750"), ("5.7.708", "other_ndr"), ("5.1.1", "other_ndr")]:
            msg = {"subject": "Undeliverable: probe-123", "body": {"content": f"Delivery has failed. {code}"}}
            self.assertEqual(classify_ndr([msg], "probe-123")["status"], expected)

    def test_no_clean_result_after_failed_scan_or_uncertain_send(self):
        tenant = {"id": "test", "number": "1", "admin": "admin@example.com"}
        class FakeGraph:
            def request(self, *args):
                return {"value": []}
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.update(tenant, sent_at="2026-11-03T00:00:00+00:00", subject="probe", send_accepted=False)
            with patch("app.services.readiness_engine.time.monotonic", side_effect=[0, 0, 1, 1]):
                monitor(FakeGraph(), tenant, store, 0.001)
            self.assertEqual(store.state["test"]["status"], "send_uncertain")
            store.update(tenant, send_accepted=True)
            with patch("app.services.readiness_engine.get_messages", side_effect=GraphError("permission denied")):
                with self.assertRaises(GraphError):
                    monitor(FakeGraph(), tenant, store, 0.001)
            self.assertNotEqual(store.state["test"]["status"], "no_ndr_observed")


if __name__ == "__main__":
    unittest.main()
