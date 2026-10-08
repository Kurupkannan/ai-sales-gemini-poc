"""Create or promote an explicitly selected Firebase identity to platform admin.

Run from be/: uv run python -m scripts.bootstrap_admin --email you@example.com --create
Passwords are prompted privately; no password is accepted as a command argument.
"""
import argparse
import asyncio
import getpass
import sys
import warnings
from uuid import UUID

from email_validator import validate_email, EmailNotValidError
from firebase_admin import auth
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from app.auth.firebase import firebase_app
from app.auth.permissions import claims_for_user
from app.config import settings
from app.db.models.identity import User, AuditEvent, Organization, Membership
from app.db.session import database


class BootstrapError(Exception):
    """Operator-facing error that never includes credentials or provider responses."""


def email_argument(value):
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError:
        raise argparse.ArgumentTypeError('Enter a valid email address.') from None


def arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='Create or promote a main super admin in Firebase and PostgreSQL.',
        epilog='Run from be/ so .env is loaded. Existing passwords and email verification are never changed.',
    )
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument('--email', type=email_argument, help='Email of the Firebase account to promote')
    identity.add_argument('--uid', help='Existing Firebase UID (alternative to --email)')
    parser.add_argument('--create', action='store_true', help='Create the email account if missing; securely prompts for a password')
    parser.add_argument('--name', help='Display name for a newly created account only')
    parser.add_argument('--organization', type=UUID, help='Also assign org admin membership to this existing organization')
    args = parser.parse_args(argv)
    if args.create and not args.email:
        parser.error('--create requires --email')
    if args.name and (not args.create or not args.name.strip() or len(args.name) > 200):
        parser.error('--name requires --create and a nonempty name of at most 200 characters')
    if args.uid is not None and not 1 <= len(args.uid.strip()) <= 128:
        parser.error('--uid must contain 1–128 characters')
    return args


def new_password():
    if not sys.stdin.isatty():
        raise BootstrapError('Creating an account requires an interactive terminal for the hidden password prompt.')
    # Do not let getpass silently fall back to echoing input if /dev/tty fails.
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            password = getpass.getpass('New admin password (at least 12 characters): ')
            confirm = getpass.getpass('Confirm password: ')
        except (getpass.GetPassWarning, EOFError):
            raise BootstrapError('Could not read a hidden password. Run this command in an interactive terminal.') from None
    if len(password) < 12 or len(password) > 128:
        raise BootstrapError('Use a password between 12 and 128 characters.')
    if password != confirm:
        raise BootstrapError('Passwords do not match. No account was created.')
    return password


def resolve_account(args, app):
    try:
        record = auth.get_user(args.uid, app=app) if args.uid else auth.get_user_by_email(args.email, app=app)
    except auth.UserNotFoundError:
        if not args.create:
            raise BootstrapError('Firebase account not found. Use --email ADDRESS --create to create one.') from None
        password = new_password()
        try:
            record = auth.create_user(
                email=args.email, password=password, display_name=args.name.strip() if args.name else None,
                email_verified=False, disabled=False, app=app,
            )
        except auth.EmailAlreadyExistsError:
            raise BootstrapError('This email was just registered elsewhere. Retry without --create to select the existing account.') from None
        finally:
            password = None
        print('Firebase account created. If a later step fails, rerun this command with the same email.')
    if record.disabled:
        raise BootstrapError('This Firebase account is disabled. Resolve its suspension before granting admin access.')
    if not record.email:
        raise BootstrapError('The selected Firebase account must have an email address.')
    return record


async def preflight(organization):
    """Check local prerequisites BEFORE creating an external Firebase account."""
    async with database.transaction() as session:
        await session.execute(select(User.id).limit(0))
        await session.execute(select(AuditEvent.id).limit(0))
        if organization and not await session.get(Organization, organization):
            raise BootstrapError('Organization not found. No account was created or promoted.')


async def grant_admin(record, organization=None):
    async with database.transaction() as session:
        # Match management API lock order: organization before user.
        if organization:
            org = await session.scalar(select(Organization).where(Organization.id == organization).with_for_update())
            if not org:
                raise BootstrapError('Organization no longer exists. No database changes committed.')
        await session.execute(insert(User).values(
            firebase_uid=record.uid, email=record.email.lower(), display_name=record.display_name or '',
        ).on_conflict_do_nothing(index_elements=[User.firebase_uid]))
        user = await session.scalar(select(User).where(User.firebase_uid == record.uid).with_for_update())
        if not user.active:
            raise BootstrapError('This application account is suspended. The bootstrap CLI will not reactivate it.')
        changed = not user.platform_admin
        user.email = record.email.lower()
        user.platform_admin = True
        if changed:
            user.permissions_version += 1
            session.add(AuditEvent(actor_id=user.id, action='platform_admin.bootstrapped', target=str(user.id),
                details={'source': 'operator_cli', 'email_verified': record.email_verified}))
        if organization:
            member = await session.scalar(select(Membership).where(
                Membership.organization_id == organization, Membership.user_id == user.id))
            if not member or member.role != 'org_admin' or member.status != 'active':
                if not member:
                    member = Membership(organization_id=organization, user_id=user.id, role='org_admin')
                    session.add(member)
                member.role = 'org_admin'
                member.status = 'active'
                user.permissions_version += 1
                session.add(AuditEvent(actor_id=user.id, organization_id=organization,
                    action='membership.operator_provisioned', target=str(user.id), details={'source': 'operator_cli'}))
        return changed


async def sync_claims(uid, app):
    """Sync this identity only; never run unrelated email jobs or change passwords."""
    async with database.transaction() as session:
        user = await session.scalar(select(User).where(User.firebase_uid == uid).with_for_update())
        if not user or not user.active or not user.platform_admin:
            raise BootstrapError('Admin access changed during provisioning. Recheck the account.')
        await run_in_threadpool(auth.set_custom_user_claims, uid, claims_for_user(user), app=app)
        user.claims_synced_version = user.permissions_version
        user.claims_retry_at = None


async def mark_claims_pending(uid):
    async with database.transaction() as session:
        user = await session.scalar(select(User).where(User.firebase_uid == uid).with_for_update())
        if user:
            user.claims_synced_version = 0
            user.claims_retry_at = None


async def run(args):
    try:
        await preflight(args.organization)
        app = firebase_app()
        print(f'Firebase project: {app.project_id}')
        record = await run_in_threadpool(resolve_account, args, app)
        try:
            changed = await grant_admin(record, args.organization)
        except BootstrapError:
            raise
        except Exception:
            raise BootstrapError('Database provisioning failed. The Firebase account is retained; fix the database and rerun with the same email or UID.') from None
        print('Super-admin database access granted.' if changed else 'This account is already a super admin.')
        print(f'Email: {record.email}\nFirebase UID: {record.uid}')
        if not record.email_verified:
            print('Email verification is still required: sign in to the frontend, choose Resend email, verify, then continue.')
        if settings.AUTH_REQUIRE_ADMIN_MFA:
            print('Admin MFA is required: enroll under Security and sign in again with your authenticator.')
        if args.organization:
            print('Organization admin membership assigned. Organization status was not changed.')
        try:
            await sync_claims(record.uid, app)
        except Exception:
            try:
                await mark_claims_pending(record.uid)
            except Exception:
                pass  # Rerunning this CLI remains safe if the database is temporarily unavailable.
            print('Database access is saved, but Firebase claims synchronization failed. Rerun this command or run the auth worker; access checks still use PostgreSQL.', file=sys.stderr)
            return 2
        print('Firebase claims synchronized. Sign out and sign in to refresh your session.')
        return 0
    finally:
        await database.dispose()


def cli(argv=None):
    args = arguments(argv)
    try:
        return asyncio.run(run(args))
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. If Firebase creation already completed, rerun with the same email.', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'Admin setup failed ({type(exc).__name__}). Check backend Firebase credentials and database migrations. Provider details are omitted to protect secrets.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
