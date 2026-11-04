from unittest.mock import Mock
import pytest
from app.services import microsoft_mfa
from app.services import powershell_exchange


@pytest.mark.asyncio
async def test_mailbox_connection_uses_verified_mfa_token_over_stdin(monkeypatch):
    acquire=Mock(return_value='synthetic-access-token')
    monkeypatch.setattr(microsoft_mfa,'get_token',acquire)
    process=Mock()
    process.stdout.readline.side_effect=['module ready\n','\x1b[?1lMFA_CONNECTED_SUCCESS\n']
    popen=Mock(return_value=process)
    monkeypatch.setattr(powershell_exchange.subprocess,'Popen',popen)
    driver=Mock()
    service=powershell_exchange.PowerShellExchangeService(driver,'admin@example.onmicrosoft.com','synthetic-password','synthetic-seed')
    assert await service.connect()
    assert service.connected
    acquire.assert_called_once_with('admin@example.onmicrosoft.com','synthetic-password','synthetic-seed',driver=driver)
    assert 'synthetic-access-token' not in ' '.join(popen.call_args.args[0])
    command=process.stdin.write.call_args.args[0]
    assert '-AccessToken' in command and 'Get-ConnectionInformation' in command
    assert '-Device' not in command and 'synthetic-password' not in command and 'synthetic-seed' not in command
    process.terminate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['MFA_CONNECTION_FAILED\n',TimeoutError()])
async def test_unconfirmed_mailbox_connection_fails_closed(monkeypatch,outcome):
    monkeypatch.setattr(microsoft_mfa,'get_token',Mock(return_value='synthetic-access-token'))
    process=Mock()
    if isinstance(outcome,Exception):process.stdout.readline.side_effect=outcome
    else:process.stdout.readline.return_value=outcome
    monkeypatch.setattr(powershell_exchange.subprocess,'Popen',Mock(return_value=process))
    service=powershell_exchange.PowerShellExchangeService(Mock(),'admin@example.onmicrosoft.com','synthetic-password','synthetic-seed')
    assert not await service.connect()
    assert service.connected is False
    process.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_echoed_command_text_cannot_confirm_exchange_connection(monkeypatch):
    monkeypatch.setattr(microsoft_mfa,'get_token',Mock(return_value='synthetic-access-token'))
    process=Mock()
    process.stdout.readline.side_effect=["Write-Output 'MFA_CONNECTED_SUCCESS'\n",'MFA_CONNECTION_FAILED\n']
    monkeypatch.setattr(powershell_exchange.subprocess,'Popen',Mock(return_value=process))
    service=powershell_exchange.PowerShellExchangeService(Mock(),'admin@example.onmicrosoft.com','synthetic-password','synthetic-seed')
    assert not await service.connect()
    assert service.connected is False
