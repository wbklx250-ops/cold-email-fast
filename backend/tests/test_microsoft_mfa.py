import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import microsoft_mfa as auth


def token(tenant='tenant-id', account='admin@example.onmicrosoft.com'):
    return {'access_token':'synthetic-access-token','id_token_claims':{'tid':tenant,'preferred_username':account}}


@pytest.fixture
def application(monkeypatch):
    app=Mock()
    app.get_accounts.return_value=[]
    app.initiate_device_flow.return_value={'user_code':'TESTONLY1','expires_at':9999999999}
    app.acquire_token_by_device_flow.return_value=token()
    monkeypatch.setattr(auth,'_apps',{})
    monkeypatch.setattr(auth,'_verified_apps',{})
    monkeypatch.setattr(auth,'_locks',{})
    monkeypatch.setattr(auth,'_identity',lambda *args:('tenant-id','tenant-id','synthetic-seed'))
    monkeypatch.setattr(auth.msal,'PublicClientApplication',Mock(return_value=app))
    return app


def test_device_auth_restores_the_existing_wizard_tab(application,monkeypatch):
    complete=Mock()
    monkeypatch.setattr(auth,'_drive_signin',complete)
    driver=Mock(current_window_handle='wizard')
    driver.timeouts=SimpleNamespace(implicit_wait=15)
    assert auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=driver)=='synthetic-access-token'
    assert complete.call_args.args[4]=='TESTONLY1'
    driver.switch_to.new_window.assert_called_once_with('tab')
    driver.close.assert_called_once()
    driver.switch_to.window.assert_called_once_with('wizard')
    assert [call.args[0] for call in driver.implicitly_wait.call_args_list]==[0,15]


def test_wrong_tenant_cannot_reuse_a_cached_token(application):
    application.get_accounts.return_value=[{}]
    application.acquire_token_silent.return_value=token(tenant='other-tenant')
    with pytest.raises(auth.MicrosoftAuthenticationError,match='different administrator or tenant'):
        auth.get_token('admin@example.onmicrosoft.com','synthetic-password')
    application.initiate_device_flow.assert_not_called()


def test_failed_device_auth_still_restores_wizard(application,monkeypatch):
    monkeypatch.setattr(auth,'_drive_signin',Mock(side_effect=auth.MicrosoftAuthenticationError('Synthetic sign-in failure')))
    driver=Mock(current_window_handle='wizard')
    driver.timeouts=SimpleNamespace(implicit_wait=15)
    with pytest.raises(auth.MicrosoftAuthenticationError):
        auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=driver)
    driver.close.assert_called_once()
    driver.switch_to.window.assert_called_once_with('wizard')
    assert [call.args[0] for call in driver.implicitly_wait.call_args_list]==[0,15]


def test_msal_cache_hit_without_id_claims_reuses_only_verified_account(application,monkeypatch):
    monkeypatch.setattr(auth,'_drive_signin',Mock())
    driver=Mock(current_window_handle='wizard')
    auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=driver)
    application.get_accounts.return_value=[{'username':'ADMIN@example.onmicrosoft.com','realm':'tenant-id'}]
    application.acquire_token_silent.return_value={'access_token':'synthetic-cached-token','token_source':'cache'}
    assert auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=driver)=='synthetic-cached-token'
    application.initiate_device_flow.assert_called_once()
    driver.switch_to.new_window.assert_called_once()


@pytest.mark.parametrize('account',[
    {'username':'other@example.onmicrosoft.com','realm':'tenant-id'},
    {'username':'admin@example.onmicrosoft.com','realm':'other-tenant'},
])
def test_claimless_cache_cannot_switch_verified_account(application,monkeypatch,account):
    monkeypatch.setattr(auth,'_drive_signin',Mock())
    driver=Mock(current_window_handle='wizard')
    auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=driver)
    application.get_accounts.return_value=[account]
    application.acquire_token_silent.return_value={'access_token':'synthetic-cached-token'}
    with pytest.raises(auth.MicrosoftAuthenticationError,match='different administrator or tenant'):
        auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=driver)


def test_claimless_unverified_cache_requires_device_identity_validation(application,monkeypatch):
    monkeypatch.setattr(auth,'_drive_signin',Mock())
    application.get_accounts.return_value=[{'username':'admin@example.onmicrosoft.com','realm':'tenant-id'}]
    application.acquire_token_silent.return_value={'access_token':'unverified-cached-token'}
    assert auth.get_token('admin@example.onmicrosoft.com','synthetic-password',driver=Mock())=='synthetic-access-token'
    application.initiate_device_flow.assert_called_once()


def test_graph_readback_requests_only_delegated_read_scopes(application):
    application.get_accounts.return_value=[{}]
    application.acquire_token_silent.return_value=token()
    assert auth.get_token('admin@example.onmicrosoft.com','synthetic-password',resource='graph')
    scopes=application.acquire_token_silent.call_args.args[0]
    assert set(scopes)=={'https://graph.microsoft.com/Domain.Read.All','https://graph.microsoft.com/User.Read'}


def test_mailbox_user_permissions_are_separate_from_readonly_domain_permissions(application):
    application.get_accounts.return_value=[{}]
    application.acquire_token_silent.return_value=token()
    assert auth.get_token('admin@example.onmicrosoft.com','synthetic-password',resource='graph_users')
    assert set(application.acquire_token_silent.call_args.args[0])=={
        'https://graph.microsoft.com/User.ReadWrite.All','https://graph.microsoft.com/User.EnableDisableAccount.All',
        'https://graph.microsoft.com/User-PasswordProfile.ReadWrite.All','https://graph.microsoft.com/User.Read'}


def test_exchange_uses_access_token_over_stdin_and_preserves_exact_targets(monkeypatch):
    monkeypatch.setattr(auth,'get_token',lambda *args,**kwargs:'synthetic-access-token')
    value={'success':True,'exists':True,'enabled':False,'selector1':'selector1-example-com._domainkey.tenant.p-v1.dkim.mail.microsoft',
        'selector2':'selector2-example-com._domainkey.tenant.p-v1.dkim.mail.microsoft','error':None}
    run=Mock(return_value=SimpleNamespace(stdout='\x1b[?1hREADBACK_BEGIN\n'+json.dumps(value)+'\nREADBACK_END'))
    monkeypatch.setattr(auth.subprocess,'run',run)
    result=auth.read_dkim('admin@example.onmicrosoft.com','synthetic-password','example.com','synthetic-seed',create=True)
    assert result['selector1']==value['selector1']
    assert result['enabled'] is False
    assert result['ok'] is False
    command=run.call_args.args[0]
    script=run.call_args.kwargs['input']
    assert 'synthetic-access-token' not in ' '.join(command)
    assert '-AccessToken' in script and '-Credential' not in script
    assert 'synthetic-password' not in script and 'synthetic-seed' not in script


def test_invalid_domain_cannot_become_a_powershell_command(monkeypatch):
    acquire=Mock()
    monkeypatch.setattr(auth,'get_token',acquire)
    with pytest.raises(ValueError):
        auth.read_dkim('admin@example.onmicrosoft.com','synthetic-password',"example.com'; Get-Secret",create=True)
    acquire.assert_not_called()


def test_fresh_device_signin_handles_username_password_and_existing_code(monkeypatch):
    from selenium.webdriver.common.by import By
    stage={'value':'device'}
    entered=[]
    class Field:
        def __init__(self,name):self.name=name
        def is_displayed(self):return self.name!='transparent-password'
        def is_enabled(self):return True
        def get_attribute(self,name):return None
        def clear(self):pass
        def send_keys(self,value):entered.append((self.name,value))
        def click(self):stage['value']={'device':'username','username':'password','password':'approve','approve':'mfa','mfa':'complete'}[stage['value']]
    class Body:
        @property
        def text(self):
            if stage['value']=='approve':
                return 'admin@example.onmicrosoft.com\nApprove sign-in on another device\nMicrosoft Exchange REST API Based Powershell'
            return 'Sign in'
    body=Body()
    class Driver:
        current_url='https://login.microsoftonline.com/deviceauth'
        def get(self,url):pass
        def find_element(self,*args):return body
        def find_elements(self,by,selector):
            if selector=='#otc, input[name=otc]' and stage['value']=='device':return [Field('device')]
            if selector.startswith('#idSIButton9'):return [Field('submit')]
            if selector=='#idBtn_Approve' and stage['value']=='approve':return [Field('approve')]
            if 'input[name=loginfmt]' in selector and stage['value']=='username':return [Field('username')]
            if 'input[name=passwd]' in selector:
                return [Field('password')] if stage['value']=='password' else [Field('transparent-password')]
            if '#idTxtBx_SAOTCC_OTC' in selector and stage['value']=='mfa':return [Field('mfa')]
            return []
    monkeypatch.setattr(auth.time,'sleep',lambda _:None)
    monkeypatch.setattr(auth.time,'time',lambda:120)
    auth._drive_signin(Driver(),'admin@example.onmicrosoft.com','synthetic-password','JBSWY3DPEHPK3PXP','TESTONLY1',lambda:stage['value']=='complete')
    assert [name for name,_ in entered]==['device','username','password','mfa']


@pytest.mark.asyncio
async def test_exchange_signing_readback_uses_mfa_instead_of_password_auth(monkeypatch):
    from app.services import objective_reconciliation
    lookup=Mock(return_value={'success':True,'enabled':True})
    monkeypatch.setattr(auth,'read_dkim',lookup)
    legacy=AsyncMock(side_effect=AssertionError('Password-only Exchange auth must not run'))
    monkeypatch.setattr(objective_reconciliation,'_run_powershell',legacy)
    data={'name':'example.com','tenant':{'admin_email':'admin@example.onmicrosoft.com',
        'admin_password':'synthetic-password','totp_secret':'synthetic-seed'}}
    assert (await objective_reconciliation._read_dkim_truth(data,enable=True))['enabled']
    lookup.assert_called_once_with('admin@example.onmicrosoft.com','synthetic-password','example.com','synthetic-seed',create=False,enable=True)
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_wizard_graph_readback_uses_saved_mfa_key(monkeypatch):
    import httpx
    from app.services import m365_setup
    acquire=Mock(return_value='synthetic-access-token')
    monkeypatch.setattr(auth,'get_token',acquire)
    legacy=AsyncMock(side_effect=AssertionError('Password grant must not run for MFA account'))
    monkeypatch.setattr(m365_setup,'_get_readonly_graph_token',legacy)
    client=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'isVerified':True,'supportedServices':['Email']}))
    monkeypatch.setattr(m365_setup.httpx,'AsyncClient',lambda **kwargs:client(transport=transport,**kwargs))
    result=await m365_setup._read_post_wizard_domain_truth('example.com',None,{
        'admin_email':'admin@example.onmicrosoft.com','admin_password':'synthetic-password','totp_secret':'synthetic-seed'})
    assert result['ok']
    acquire.assert_called_once_with('admin@example.onmicrosoft.com','synthetic-password','synthetic-seed',resource='graph')
    legacy.assert_not_awaited()
