"""Exercise the actual browser controls used by automatic TOTP registration."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pyotp
from playwright.sync_api import sync_playwright
from app.services.readiness_worker import CloudBrowser as Browser, cloud_secret as protect_password
from app.services.readiness_engine import MFASetupError, ROOT

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
