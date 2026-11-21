"""Organization-authorized outbound Plivo calls."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from app.auth.dependencies import require_permission
from app.auth.calls import read_call, calls_for_lead
from app.config import settings
from app.telephony.service import initiate, terminate
from app.telephony import dialer

router = APIRouter()


class OutboundCallRequest(BaseModel):
    contact_id: str
    idempotency_key: str = Field(min_length=16, max_length=100)
    to_number: str = Field(min_length=8, max_length=30, description='Authorized contact number, including +countrycode')


class OutboundCallResponse(BaseModel):
    call_id: str
    attempt_id: str
    provider: str = 'plivo'
    status: str
    provider_request_id: str | None = None
    provider_call_id: str | None = None
    end_reason: str | None = None
    cleanup_pending: bool = False
    summary: dict | None = None


class BatchCallRequest(BaseModel):
    lead_ids: list[UUID] = Field(min_length=1, max_length=500)


def require_calling():
    if not settings.OUTBOUND_CALLS_ENABLED:
        raise HTTPException(503, 'Outbound calling is disabled.')
    try:
        settings.validate_plivo()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from None


@router.post('/api/orgs/{org_id}/calls', response_model=OutboundCallResponse)
async def create_call(payload: OutboundCallRequest, request: Request, identity=Depends(require_permission('calls.create'))):
    require_calling()
    return await initiate(request.state.organization_id, identity, payload)


@router.post('/api/orgs/{org_id}/calls/batch', status_code=202)
async def create_call_batch(payload: BatchCallRequest, request: Request, identity=Depends(require_permission('calls.create'))):
    """Queue many leads; they are dialed concurrently across all caller numbers."""
    require_calling()
    await resume_calling(request.state.organization_id)
    return await dialer.start(request.state.organization_id, identity, payload.lead_ids)


@router.post('/api/orgs/{org_id}/calls/new-leads', status_code=202)
async def call_new_leads(request: Request, identity=Depends(require_permission('calls.create'))):
    """One-off: queue every lead that has never been called, across all caller lines."""
    require_calling()
    await resume_calling(request.state.organization_id)
    lead_ids = await dialer.never_called_ids(request.state.organization_id)
    if not lead_ids:
        raise HTTPException(409, 'There are no new leads to call.')
    return await dialer.start(request.state.organization_id, identity, lead_ids)


async def resume_calling(org_id):
    """Starting any calling again lifts a "Stop all calling" pause (so retries resume too)."""
    from app.auth.store import store
    async with store.lock(org_id):
        org = await store.get_org(org_id, fresh=True)
        if org and org.calling_paused:
            org.calling_paused = False
            await store.save_org(org)


@router.post('/api/orgs/{org_id}/calls/stop-all')
async def stop_all_calling(request: Request, identity=Depends(require_permission('calls.create'))):
    """Emergency stop: auto-dial off and every queued call cancelled. Connected calls finish normally."""
    from app.auth.store import store
    org_id = request.state.organization_id
    async with store.lock(org_id):
        org = await store.get_org(org_id, fresh=True)
        org.auto_dial = False
        org.calling_paused = True
        await store.save_org(org)
    cancelled = dialer.stop_all(org_id)
    return {**await dialer.auto_status(org), 'cancelled': cancelled}


class AutoDialRequest(BaseModel):
    enabled: bool


@router.get('/api/orgs/{org_id}/calls/auto-dial')
async def get_auto_dial(request: Request, identity=Depends(require_permission('calls.read'))):
    from app.auth.store import store
    return await dialer.auto_status(await store.get_org(request.state.organization_id, fresh=True))


@router.put('/api/orgs/{org_id}/calls/auto-dial')
async def set_auto_dial(payload: AutoDialRequest, request: Request, identity=Depends(require_permission('calls.create'))):
    """Keep every caller line busy with leads that have never been called."""
    from app.auth.store import store
    if payload.enabled:
        require_calling()
    org_id = request.state.organization_id
    async with store.lock(org_id):
        org = await store.get_org(org_id, fresh=True)
        org.auto_dial = payload.enabled
        org.auto_dial_by = identity.id if payload.enabled else org.auto_dial_by
        org.calling_paused = org.calling_paused and not payload.enabled
        await store.save_org(org)
    if payload.enabled:
        await dialer.auto_dial_org(org)  # Start now instead of waiting for the next round.
    else:
        dialer.stop_auto(org_id)
    return await dialer.auto_status(org)


@router.get('/api/orgs/{org_id}/calls/batch/{batch_id}')
async def get_call_batch(batch_id: UUID, request: Request, identity=Depends(require_permission('calls.read'))):
    return dialer.get(request.state.organization_id, batch_id).snapshot()


@router.post('/api/orgs/{org_id}/calls/batch/{batch_id}/cancel')
async def cancel_call_batch(batch_id: UUID, request: Request, identity=Depends(require_permission('calls.create'))):
    """Stops dialing queued leads; calls already ringing or connected continue."""
    return dialer.cancel(dialer.get(request.state.organization_id, batch_id))


@router.get('/api/orgs/{org_id}/calls/{call_id}', response_model=OutboundCallResponse)
async def get_call(call_id: UUID, request: Request, identity=Depends(require_permission('calls.read'))):
    return await read_call(request.state.organization_id, call_id)


@router.post('/api/orgs/{org_id}/calls/{call_id}/stop', response_model=OutboundCallResponse)
async def stop_call(call_id: UUID, request: Request, identity=Depends(require_permission('calls.create'))):
    call = await read_call(request.state.organization_id, call_id)
    if call['status'] not in {'completed', 'failed'}:
        await terminate(UUID(call['attempt_id']), 'stopped_by_operator')
    return await read_call(request.state.organization_id, call_id)


@router.get('/api/orgs/{org_id}/leads/{lead_id}/calls')
async def lead_calls(lead_id: UUID, request: Request, identity=Depends(require_permission('calls.read'))):
    return await calls_for_lead(request.state.organization_id, lead_id)
