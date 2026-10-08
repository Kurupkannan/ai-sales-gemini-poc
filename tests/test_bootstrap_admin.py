"""CLI tests mock Firebase and database calls; never provision real accounts."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from firebase_admin import auth
from scripts import bootstrap_admin as cli
from app.auth.permissions import claims_for_user


def record(**overrides):
    return SimpleNamespace(**{'uid':'selected-user','email':'admin@example.com','display_name':'Admin',
        'disabled':False,'email_verified':False, **overrides})


@pytest.mark.parametrize('args', [[], ['--uid','a','--email','a@example.com'], ['--uid','a','--create'],
    ['--email','invalid'], ['--email','a@example.com','--name','Admin'], ['--uid',' '],
    ['--email','a@example.com','--password','never-allow-password-in-argv']])
def test_invalid_arguments(args):
    with pytest.raises(SystemExit) as exc: cli.arguments(args)
    assert exc.value.code == 2


def test_create_new_account_keeps_email_unverified(monkeypatch):
    monkeypatch.setattr(cli.auth,'get_user_by_email',Mock(side_effect=auth.UserNotFoundError('missing')))
    create=Mock(return_value=record())
    monkeypatch.setattr(cli.auth,'create_user',create)
    monkeypatch.setattr(cli,'new_password',lambda:'private-password-value')
    args=cli.arguments(['--email','admin@example.com','--create','--name','Main Admin'])
    result=cli.resolve_account(args,'app')
    assert result.uid=='selected-user'
    assert create.call_args.kwargs['email_verified'] is False
    assert create.call_args.kwargs['disabled'] is False
    assert create.call_args.kwargs['display_name']=='Main Admin'


def test_existing_account_does_not_change_password(monkeypatch):
    selected=record()
    monkeypatch.setattr(cli.auth,'get_user_by_email',Mock(return_value=selected))
    create=Mock(); password=Mock()
    monkeypatch.setattr(cli.auth,'create_user',create); monkeypatch.setattr(cli,'new_password',password)
    assert cli.resolve_account(cli.arguments(['--email','admin@example.com','--create']),'app') is selected
    create.assert_not_called(); password.assert_not_called()


def test_missing_account_requires_explicit_create(monkeypatch):
    monkeypatch.setattr(cli.auth,'get_user',Mock(side_effect=auth.UserNotFoundError('missing')))
    with pytest.raises(cli.BootstrapError,match='--create'):
        cli.resolve_account(cli.arguments(['--uid','missing']),'app')


@pytest.mark.parametrize('selected,match', [(record(disabled=True),'disabled'),(record(email=None),'email address')])
def test_refuses_disabled_or_emailless_account(monkeypatch,selected,match):
    monkeypatch.setattr(cli.auth,'get_user',Mock(return_value=selected))
    with pytest.raises(cli.BootstrapError,match=match): cli.resolve_account(cli.arguments(['--uid','selected-user']),'app')


def test_password_input_requires_private_terminal(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin,'isatty',lambda:False)
    with pytest.raises(cli.BootstrapError,match='interactive terminal'): cli.new_password()


@pytest.mark.parametrize('values', [('too-short','too-short'),('long-password-here','different-password')])
def test_password_validation(monkeypatch,values):
    monkeypatch.setattr(cli.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(cli.getpass,'getpass',Mock(side_effect=values))
    with pytest.raises(cli.BootstrapError): cli.new_password()


@pytest.fixture
def runner(monkeypatch):
    app=SimpleNamespace(project_id='test-project')
    monkeypatch.setattr(cli,'firebase_app',lambda:app)
    monkeypatch.setattr(cli,'preflight',AsyncMock())
    monkeypatch.setattr(cli,'resolve_account',Mock(return_value=record()))
    monkeypatch.setattr(cli,'grant_admin',AsyncMock(return_value=True))
    monkeypatch.setattr(cli,'sync_claims',AsyncMock())
    monkeypatch.setattr(cli,'mark_claims_pending',AsyncMock())
    monkeypatch.setattr(cli,'database',SimpleNamespace(dispose=AsyncMock()))
    return cli.arguments(['--email','admin@example.com','--create'])


async def test_success_reports_verification_without_secrets(runner,capsys):
    assert await cli.run(runner)==0
    output=capsys.readouterr().out
    assert 'verification is still required' in output
    assert 'claims synchronized' in output
    assert 'private-password' not in output
    cli.database.dispose.assert_awaited_once()


async def test_preflight_failure_does_not_create_firebase_user(runner):
    cli.preflight.side_effect=cli.BootstrapError('Database unavailable')
    with pytest.raises(cli.BootstrapError): await cli.run(runner)
    cli.resolve_account.assert_not_called()
    cli.database.dispose.assert_awaited_once()


async def test_claims_failure_keeps_db_grant_and_returns_retry_status(runner,capsys):
    cli.sync_claims.side_effect=RuntimeError('provider response containing secret')
    assert await cli.run(runner)==2
    cli.grant_admin.assert_awaited_once()
    cli.mark_claims_pending.assert_awaited_once_with('selected-user')
    output=capsys.readouterr()
    assert 'Database access is saved' in output.err
    assert 'provider response' not in output.err


def test_suspended_accounts_have_no_admin_claims():
    assert claims_for_user(SimpleNamespace(platform_admin=True,active=False,permissions_version=9))=={
        'platform_role':'user','permissions':[],'permissions_version':9}
