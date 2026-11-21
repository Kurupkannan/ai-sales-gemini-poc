"""Export scoped lead snapshots without making telephony wait for Google Sheets.

The database is authoritative. Failed writes retry on the next polling pass, and
restart backfills all leads. Snapshot timestamps include late call summaries.
"""
import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.db.models import CallSession, Conversation, Lead, LeadContact
from app.db.session import database

logger = logging.getLogger(__name__)


class SheetsError(RuntimeError):
    """Messages are fixed categories; never include provider bodies or secrets."""


def configuration():
    url = settings.GOOGLE_SHEETS_WEB_APP_URL.get_secret_value().strip()
    if not re.fullmatch(r'https://script\.google\.com/macros/s/[A-Za-z0-9_-]+/exec', url):
        raise SheetsError('invalid_web_app_url')
    if not settings.GOOGLE_SHEETS_ACCESS_CODE.get_secret_value():
        raise SheetsError('missing_access_code')
    try:
        org_id = UUID(settings.GOOGLE_SHEETS_ORGANIZATION_ID)
    except ValueError:
        raise SheetsError('invalid_export_organization') from None
    return url, org_id


def snapshot(lead, contact=None, call=None, live=None):
    """Full app-owned row; nulls clear results belonging to an earlier call."""
    summary = (live.summary if live else call.result_summary if call else None) or {}
    has_call = live is not None or call is not None
    last_at = ((live.answered_at or live.created_at) if live else
               (call.started_at or call.created_at) if call else None)
    timestamps = [lead.created_at, lead.updated_at]
    if contact:
        timestamps += [contact.created_at, contact.updated_at]
    if call:
        timestamps += [call.created_at, call.updated_at, call.started_at, call.ended_at]
    if live:
        timestamps += [live.created_at, live.last_activity, live.answered_at, live.ended_at]
    updated = max(t.astimezone(timezone.utc) for t in timestamps if t)
    return {
        'lead_id': str(lead.id), 'name': lead.name, 'phone': contact.phone_number if contact else None,
        'email': lead.email, 'source': lead.source, 'language': lead.preferred_language,
        'lead_status': lead.status, 'result': summary.get('lead_status', 'UNKNOWN' if has_call else None),
        'call_status': live.state if live else call.status if call else None,
        'provider_status': call.provider_status if call else None,
        'summary': summary.get('summary'),
        'preferred_callback_time': summary.get('preferred_callback_time'),
        'webinar_response': summary.get('webinar_response'),
        'send_webinar_link': bool(summary.get('send_webinar_link', False)),
        'genuineness': summary.get('genuineness'),
        'duration_seconds': live.bill_duration if live else call.bill_duration if call else None,
        'call_id': str(live.call_session_id) if live else str(call.id) if call else None,
        'last_call_at': last_at.isoformat() if last_at else None,
        'updated_at': updated.isoformat(),
    }


async def snapshots(org_id, after=None, page_size=100):
    # Fetch in small pages; never hold a DB connection while calling Google.
    async with database.transaction() as session:
        session.info['organization_id'] = org_id
        query = select(Lead).where(Lead.organization_id == org_id).order_by(Lead.id).limit(page_size)
        if after:
            query = query.where(Lead.id > after)
        leads = (await session.scalars(query)).all()
        if not leads:
            return [], None
        ids = [lead.id for lead in leads]
        contacts = (await session.scalars(select(LeadContact).where(
            LeadContact.organization_id == org_id, LeadContact.lead_id.in_(ids),
            LeadContact.channel == 'voice').order_by(LeadContact.created_at, LeadContact.id))).all()
        phone_for = {}
        for contact in contacts:
            phone_for.setdefault(contact.lead_id, contact)
        ranked = (select(CallSession.id.label('call_id'), Conversation.lead_id,
            func.row_number().over(partition_by=Conversation.lead_id,
                order_by=(CallSession.created_at.desc(), CallSession.id.desc())).label('position'))
            .join(Conversation, Conversation.id == CallSession.conversation_id)
            .where(CallSession.organization_id == org_id, Conversation.organization_id == org_id,
                   Conversation.channel == 'voice', Conversation.lead_id.in_(ids)).subquery())
        latest = (await session.execute(select(CallSession, ranked.c.lead_id)
            .join(ranked, ranked.c.call_id == CallSession.id).where(ranked.c.position == 1))).all()
        call_for = {lead_id: call for call, lead_id in latest}
        from app.telephony import runtime
        results = []
        for lead in leads:
            call = call_for.get(lead.id)
            live_calls = runtime.for_lead(org_id, lead.id)
            live = max(live_calls, key=lambda item: item.created_at) if live_calls else None
            # An older retained runtime object must never hide a newer durable call.
            if live and call and live.call_session_id != call.id and live.created_at <= call.created_at:
                live = None
            if live and call and live.call_session_id != call.id:
                call = None
            results.append(snapshot(lead, phone_for.get(lead.id), call, live))
        return results, leads[-1].id


class Exporter:
    def __init__(self, client):
        self.client = client
        self.synced = {}

    async def request(self, action, lead=None):
        url, _ = configuration()
        body = {'action': action, 'access_code': settings.GOOGLE_SHEETS_ACCESS_CODE.get_secret_value()}
        if lead is not None:
            body['lead'] = lead
        try:
            response = await self.client.post(url, json=body, follow_redirects=True)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise SheetsError('transport_or_deployment_error') from None
        if not isinstance(data, dict) or data.get('ok') is not True:
            allowed = {'unauthorized', 'tracker_missing', 'busy', 'duplicate_lead_ids', 'sheet_operation_failed'}
            category = data.get('error') if isinstance(data, dict) else None
            raise SheetsError(category if category in allowed else 'rejected_snapshot')
        if action == 'ping' and (data.get('service') != 'ai-sales-sheets' or data.get('version') != 3):
            raise SheetsError('deploy_script_version_3')
        if action == 'upsert_lead' and data.get('result') not in {'created', 'updated', 'skipped_stale'}:
            raise SheetsError('unexpected_write_response')
        return data

    async def sync_once(self):
        _, org_id = configuration()
        after = None
        counts = {'created': 0, 'updated': 0, 'skipped_stale': 0, 'unchanged': 0}
        while True:
            rows, after = await snapshots(org_id, after)
            if not rows:
                break
            for row in rows:
                # Audio heartbeat timestamps alone must not cause repeated exports.
                content = {k: v for k, v in row.items() if k != 'updated_at'}
                digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
                if self.synced.get(row['lead_id']) == digest:
                    counts['unchanged'] += 1
                    continue
                reply = await self.request('upsert_lead', row)
                self.synced[row['lead_id']] = digest
                counts[reply['result']] += 1
        return counts


async def run():
    # New clients use the same bounded IPv4 transport as other provider clients.
    configuration()
    async with httpx.AsyncClient(timeout=45, transport=httpx.AsyncHTTPTransport(
            local_address='0.0.0.0' if settings.NETWORK_IPV4_ONLY else None)) as client:
        exporter = Exporter(client)
        connected = False
        delay = settings.GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS
        while True:
            try:
                if not connected:
                    await exporter.request('ping')
                    connected = True
                    logger.info('Google Sheets connection verified')
                counts = await exporter.sync_once()
                if any(counts[k] for k in ('created', 'updated', 'skipped_stale')):
                    logger.info('Google Sheets export counts=%s', counts)
                delay = settings.GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS
            except Exception as exc:
                # Request exceptions can contain URLs, tokens or contact data.
                logger.warning('Google Sheets export failed category=%s; retrying',
                    str(exc) if isinstance(exc, SheetsError) else type(exc).__name__)
                connected = False
                delay = min(max(delay * 2, 15), 300)
            await asyncio.sleep(delay)
