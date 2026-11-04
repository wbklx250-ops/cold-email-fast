"""Exercise the actual browser controls used by automatic TOTP registration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pyotp
from playwright.sync_api import sync_playwright
from app.services.readiness_worker import CloudBrowser as Browser, cloud_secret as protect_password
from app.services.readiness_engine import MFASetupError, NeedsHuman, ROOT

TEST_SEED = 'JBSWY3DPEHPK3PXP'


class EnrollmentBrowserTests(unittest.TestCase):
    def setUp(self):
        test_root = ROOT / 'runtime' / 'tests'
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='enrollment-', dir=test_root, ignore_cleanup_errors=True)
        self.playwright = sync_playwright().start()
        self.browser = Browser(self.playwright, Path(self.temp.name) / 'admin-profile',
            {'id':'synthetic-test','number':'test','admin':'test@example.invalid','password':'synthetic-only'})
        self.browser.human = Mock(side_effect=AssertionError('New enrollment must not ask the operator to set up MFA.'))

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()
        self.temp.cleanup()

    def test_full_registration_captures_verifies_and_saves_totp_without_human(self):
        screens = {
            'install': '<h2>Set up your account in app</h2><p>Start by adding your account.</p><button data-step="qr">Next</button>',
            'qr': '<h2>Scan the QR code</h2><p>Use your authenticator app to scan the QR code.</p><button id="cant">Can’t scan the QR code?</button><pre id="key"></pre><button data-step="code">Next</button>',
            'code': '<h2>Enter the code</h2><p>Enter the 6-digit code from your authenticator app.</p><input aria-label="Code" maxlength="6"><button id="verify">Verify</button>',
            'added': '<h2>Authenticator app added</h2><p>Use codes from this app the next time you sign in.</p><button id="done">Done</button>',
        }
        html = '<html><body><script>const screens=' + json.dumps(screens) + ';const seed=' + json.dumps(TEST_SEED) + ';' + r"""
        function show(stage) {
          document.body.innerHTML=screens[stage];
          document.querySelectorAll('[data-step]').forEach(button => button.onclick=()=>show(button.dataset.step));
          if(stage==='qr') document.getElementById('cant').onclick=()=>document.getElementById('key').textContent='Secret key:\n'+seed+'\t\n';
          if(stage==='code') document.getElementById('verify').onclick=()=>{if(document.querySelector('input').value===window.expected)show('added')};
          if(stage==='added') document.getElementById('done').onclick=()=>window.completed=true;
        }
        show('install');
        </script></body></html>"""
        self.browser.page.set_content(html)
        for _ in range(8):
            self.browser.page.evaluate('value => window.expected = value', pyotp.TOTP(TEST_SEED).now())
            self.browser.advance_security()
            if self.browser.page.evaluate('Boolean(window.completed)'):
                break
        self.assertTrue(self.browser.page.evaluate('Boolean(window.completed)'), self.browser.page.locator('body').inner_text())
        self.assertEqual(protect_password(self.browser.totp_path), TEST_SEED)
        self.assertNotIn(TEST_SEED.encode(), self.browser.totp_path.read_bytes())
        self.assertTrue(self.browser.enrollment_completed)
        self.browser.human.assert_not_called()

    def test_existing_phone_mfa_is_distinct_from_registration(self):
        self.browser.human = Mock()
        self.browser.page.set_content('<h2>Enter code</h2><p>Enter the code displayed in the authenticator app on your mobile device.</p><input id="idTxtBx_SAOTCC_OTC" type="tel">')
        self.browser.advance_security()
        self.browser.human.assert_called_once()
        self.assertEqual(self.browser.mfa_stage, 'existing_mfa_required')

    def test_manual_key_screen_is_enrolled_automatically(self):
        self.browser.page.set_content('<h2>Enter the following into Authenticator</h2><p>Account name</p><p>test@example.invalid</p><p>Key</p><p>' + TEST_SEED + '</p><button onclick="window.nextClicked=true">Next</button>')
        self.browser.advance_security()
        self.assertEqual(protect_password(self.browser.totp_path), TEST_SEED)
        self.assertTrue(self.browser.page.evaluate('Boolean(window.nextClicked)'))
        self.browser.human.assert_not_called()

    def test_account_picker_handles_microsoft_changing_address_capitalization(self):
        html='<h1>Pick an account</h1><div role="button" onclick="window.accountSelected=true">test@EXAMPLE.invalid</div><div role="button">Use another account</div>'
        self.browser.page.route('https://login.microsoftonline.com/readiness-fixture', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
        self.browser.page.goto('https://login.microsoftonline.com/readiness-fixture')
        self.browser.advance_login()
        self.assertTrue(self.browser.page.evaluate('Boolean(window.accountSelected)'))
        self.browser.human.assert_not_called()

    def test_username_login_accepts_microsoft_text_loginfmt_input(self):
        html='<h1>Sign in</h1><form onsubmit="event.preventDefault();window.submitted=true"><input name="loginfmt" type="text" autocomplete="username"><button type="submit">Next</button></form>'
        self.browser.page.route('https://login.microsoftonline.com/readiness-login', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
        self.browser.page.goto('https://login.microsoftonline.com/readiness-login')
        self.browser.advance_login()
        self.assertEqual(self.browser.page.locator('input[name=loginfmt]').input_value(), 'test@example.invalid')
        self.assertTrue(self.browser.page.evaluate('Boolean(window.submitted)'))

    def test_password_step_preserves_readonly_username(self):
        html='<h1>Enter password</h1><form onsubmit="event.preventDefault();window.submitted=true"><input name="loginfmt" type="email" value="test@example.invalid" readonly><input name="passwd" type="password"><button type="submit">Sign in</button></form>'
        self.browser.page.route('https://login.microsoftonline.com/readiness-password', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
        self.browser.page.goto('https://login.microsoftonline.com/readiness-password')
        self.browser.advance_login()
        self.assertEqual(self.browser.page.locator('input[name=passwd]').input_value(), 'synthetic-only')
        self.assertEqual(self.browser.page.locator('input[name=loginfmt]').input_value(), 'test@example.invalid')
        self.assertTrue(self.browser.page.evaluate('Boolean(window.submitted)'))

    def test_username_step_ignores_microsoft_transparent_password_autofill_field(self):
        html='''<h1>Sign in</h1><form onsubmit="event.preventDefault();window.submitted=true">
          <input name="loginfmt" type="email"><div aria-hidden="true" style="opacity:0">
          <input name="passwd" type="password"></div><button type="submit">Next</button></form>'''
        self.browser.page.route('https://login.microsoftonline.com/readiness-login', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
        self.browser.page.goto('https://login.microsoftonline.com/readiness-login')
        self.assertTrue(self.browser.page.locator('input[name=passwd]').is_visible())
        self.browser.advance_login()
        self.assertEqual(self.browser.page.locator('input[name=loginfmt]').input_value(), 'test@example.invalid')
        self.assertEqual(self.browser.page.locator('input[name=passwd]').input_value(), '')
        self.assertTrue(self.browser.page.evaluate('Boolean(window.submitted)'))

    def test_transparent_autofill_field_is_ignored_without_aria_hidden(self):
        html='''<form onsubmit="event.preventDefault();window.submitted=true">
          <input name="loginfmt" type="email"><input name="passwd" type="password" style="opacity:0">
          <button type="submit">Next</button></form>'''
        self.browser.page.route('https://login.microsoftonline.com/readiness-login', lambda route: route.fulfill(status=200,content_type='text/html',body=html))
        self.browser.page.goto('https://login.microsoftonline.com/readiness-login')
        self.browser.advance_login()
        self.assertEqual(self.browser.page.locator('input[name=loginfmt]').input_value(), 'test@example.invalid')
        self.assertEqual(self.browser.page.locator('input[name=passwd]').input_value(), '')

    def test_trial_selection_waits_for_async_dropdown_items(self):
        self.browser.page.set_content('''<button role="combobox" aria-label="Select a plan" onclick="setTimeout(()=>{document.getElementById('options').innerHTML='<button role=option onclick=window.trialChosen=true>Microsoft 365 Business Basic (Trial)</button>'},200)">Choose one from below</button><div id="options"></div>''')
        self.assertIn('(Trial)', self.browser.select_trial_plan())
        self.assertTrue(self.browser.page.evaluate('Boolean(window.trialChosen)'))

    def test_trial_selection_does_not_choose_a_disabled_offer(self):
        self.browser.page.set_content('''<button role="combobox" aria-label="Select a plan">Choose one</button><button role="option" disabled>Basic no Teams (Trial)</button><button role="option" onclick="window.trialChosen=true">Basic (Trial)</button>''')
        self.assertEqual(self.browser.select_trial_plan(), 'Basic (Trial)')
        self.assertTrue(self.browser.page.evaluate('Boolean(window.trialChosen)'))

    def test_trial_selection_handles_an_animated_fluent_popup(self):
        self.browser.page.set_content('''<style>@keyframes move {from {transform:translateX(0)} to {transform:translateX(8px)}} [role=option] {animation:move .3s infinite alternate}</style><button role="combobox" aria-label="Select a plan">Choose one</button><button role="option" onclick="window.trialChosen=true">Basic (Trial)</button>''')
        self.assertEqual(self.browser.select_trial_plan(), 'Basic (Trial)')
        self.assertTrue(self.browser.page.evaluate('Boolean(window.trialChosen)'))

    def test_renewal_terms_ignore_controls_behind_the_checkout_dialog(self):
        self.browser.page.set_content('''<html><body>
        <button role="combobox" aria-label="Billing frequency" onclick="window.backgroundTouched=true">Pay monthly</button>
        <div role="dialog" style="position:fixed;inset:0;background:white;z-index:100">
          <h2>Trial renewal settings</h2>
          <input type="radio" name="term" id="term-year" checked><label for="term-year">1 year</label>
          <input type="radio" name="term" id="term-month"><label for="term-month">1 month</label>
          <input type="radio" name="frequency" id="freq-year" checked><label for="freq-year">Pay yearly</label>
          <input type="radio" name="frequency" id="freq-month"><label for="freq-month">Pay monthly</label>
          <input type="checkbox" id="renew" checked><label for="renew">Recurring billing</label>
        </div></body></html>''')
        self.browser.shortest_terms()
        self.assertFalse(self.browser.page.evaluate('Boolean(window.backgroundTouched)'))
        self.assertTrue(self.browser.page.locator('#term-month').is_checked())
        self.assertTrue(self.browser.page.locator('#freq-month').is_checked())
        self.assertTrue(self.browser.page.locator('#renew').is_checked())

    def _exercise_checkout(self, authorized, intermediate=False):
        self.browser.accept_trial_recurring_payments = authorized
        checkout = '<h2>Checkout</h2><p>Total due: NZ$0.00</p><button onclick="renewal()">Next</button>'
        renewal = '''<h2>Trial renewal settings</h2>
          <input type="radio" name="term" id="year" checked><label for="year">1 year</label>
          <input type="radio" name="term" id="month"><label for="month">1 month</label>
          <input type="radio" name="freq" id="annual" checked><label for="annual">Pay yearly</label>
          <input type="radio" name="freq" id="monthly"><label for="monthly">Pay monthly</label>
          <input type="checkbox" id="renew" checked><label for="renew">Recurring billing</label>
          <input type="checkbox" id="consent" onchange="document.getElementById('place-order').disabled=!this.checked"><label for="consent">By checking the box, you authorize recurring payments that will continue unless canceled.</label>
          <button id="place-order" disabled onclick="order()">Place order</button>'''
        first_renewal = '<h2>Trial renewal settings</h2><button onclick="showConsent()">Start trial</button>' if intermediate else renewal
        self.browser.page.set_content('<html><body><button role="combobox" aria-label="Select a plan">Basic (Trial)</button><button onclick="checkout()">Start free trial</button><script>const checkoutHTML=' + json.dumps(checkout) + ';const renewalHTML=' + json.dumps(renewal) + ';const firstRenewalHTML=' + json.dumps(first_renewal) + ';' + r'''
          function checkout() {
            const background = document.createElement('button');
            background.setAttribute('role','combobox');background.setAttribute('aria-label','Billing frequency');
            background.textContent='Pay monthly';background.onclick=()=>window.backgroundTouched=true;
            document.body.append(background);
            const panel=document.createElement('div');panel.id='checkout';
            panel.style='position:fixed;inset:0;background:white;z-index:100';panel.innerHTML=checkoutHTML;document.body.append(panel);
          }
          function renewal(){document.getElementById('checkout').innerHTML=firstRenewalHTML}
          function showConsent(){document.getElementById('checkout').innerHTML=renewalHTML}
          function order(){window.orderClickCount=(window.orderClickCount||0)+1;window.ordered=document.getElementById('month').checked && document.getElementById('monthly').checked && document.getElementById('renew').checked && document.getElementById('consent').checked}
        </script></body></html>''')
        graph = Mock()
        sku = {'skuId':'synthetic', 'skuPartNumber':'O365_BUSINESS_ESSENTIALS', 'capabilityStatus':'Enabled',
               'prepaidUnits':{'enabled':25}, 'consumedUnits':0,
               'servicePlans':[{'servicePlanId':'exchange','servicePlanName':'EXCHANGE_S_STANDARD','provisioningStatus':'Success'}]}
        def poll(*args):
            if graph.request.call_count > 5:
                raise AssertionError('The checkout fixture did not reach its final order button.')
            return {'value':[sku] if self.browser.page.evaluate('Boolean(window.ordered)') else []}
        graph.request.side_effect = poll
        stages = []
        def progress(stage):
            stages.append(stage)
            if stage == 'trial_order_submitting':
                self.assertTrue((Path(self.temp.name) / 'trial-order-pending.json').exists())
                self.assertFalse(self.browser.page.evaluate('Boolean(window.ordered)'))
        with patch.object(self.browser, 'portal'), patch.object(self.browser, 'navigate'), patch.object(self.browser, 'advance_login'):
            if authorized:
                self.assertTrue(self.browser.start_trial(graph, progress))
            else:
                with self.assertRaises(NeedsHuman):
                    self.browser.start_trial(graph, progress)
        if not authorized:
            self.assertFalse(self.browser.page.locator('#consent').is_checked())
            self.assertFalse(self.browser.page.evaluate('Boolean(window.ordered)'))
            self.assertFalse((Path(self.temp.name) / 'trial-order-pending.json').exists())
            self.assertIn('needs_renewal_authorization', stages)
            return
        self.assertTrue(self.browser.page.evaluate('Boolean(window.ordered)'))
        self.assertFalse(self.browser.page.evaluate('Boolean(window.backgroundTouched)'))
        self.assertEqual(stages.count('trial_order_submitting'), 2 if intermediate else 1)
        self.assertEqual(stages.count('trial_order_awaiting_confirmation'), 2 if intermediate else 1)
        self.assertEqual(self.browser.page.evaluate('window.orderClickCount'), 1)
        self.assertIn('trial_zero_due_verified', stages)
        self.assertIn('trial_renewal_settings', stages)
        self.assertIn('trial_recurring_payment_consent_accepted', stages)
        self.assertTrue(self.browser.page.locator('#renew').is_checked())

    def test_zero_due_checkout_completes_with_authorized_consent_in_a_panel_without_dialog_role(self):
        self._exercise_checkout(True)

    def test_recurring_payment_consent_is_never_assumed(self):
        self._exercise_checkout(False)

    def test_intermediate_start_trial_returns_to_the_required_consent_step(self):
        self._exercise_checkout(True, intermediate=True)

    def test_order_rejection_outside_the_checkout_panel_is_visible_to_the_runner(self):
        self.browser.page.set_content('''<div role="alert">The maximum number of subscriptions to this product have already been redeemed.</div><section>
          <h2>Trial renewal settings</h2><button>Place order</button>
        </section>''')
        self.assertNotIn('already been redeemed', self.browser.checkout_scope().inner_text())
        self.assertIn('already been redeemed', self.browser.checkout_feedback())

    def test_renewal_dialog_cannot_submit_without_a_verified_zero_due_checkout(self):
        self.browser.page.set_content('<div role="dialog"><h2>Trial renewal settings</h2><button onclick="window.ordered=true">Place order</button></div>')
        graph = Mock()
        graph.request.return_value = {'value':[]}
        with patch.object(self.browser, 'portal'), patch.object(self.browser, 'navigate'), patch.object(self.browser, 'advance_login'):
            with self.assertRaises(NeedsHuman):
                self.browser.start_trial(graph, Mock())
        self.assertFalse(self.browser.page.evaluate('Boolean(window.ordered)'))
        self.assertFalse((Path(self.temp.name) / 'trial-order-pending.json').exists())

    def test_enrollment_timeout_never_becomes_a_manual_setup_prompt(self):
        self.browser.enrolling = True
        self.browser.mfa_stage = 'totp_registration'
        with patch.object(self.browser, 'navigate'), patch('app.services.readiness_engine.time.monotonic', side_effect=[0,301]):
            with self.assertRaises(MFASetupError):
                self.browser.portal()
        self.browser.human.assert_not_called()

    def test_arbitrary_page_words_are_not_accepted_as_authenticator_seeds(self):
        self.browser.page.set_content('<h2>Scan the QR code</h2><p>Use your authenticator app.</p><p>AuthenticationConfiguration</p><script>window.unrelated="secret=ABCDEFGHIJKLMNOPQ";</script>')
        self.browser.advance_security()
        self.assertIsNone(self.browser.totp)
        self.assertFalse(self.browser.totp_path.exists())
        self.browser.human.assert_not_called()


if __name__ == '__main__':
    unittest.main()
