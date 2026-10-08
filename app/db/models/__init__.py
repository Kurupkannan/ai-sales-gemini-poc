from app.db.models.import_batch import ImportBatch
"""Import all models so Alembic sees the complete schema."""

from app.db.models.lead import Lead
from app.db.models.contact import LeadContact
from app.db.models.consent import ConsentEvent, ContactPreference
from app.db.models.conversation import Conversation
from app.db.models.call import CallSession, TranscriptTurn

__all__ = [
    "ImportBatch",
    "Lead",
    "LeadContact",
    "ConsentEvent",
    "ContactPreference",
    "Conversation",
    "CallSession",
    "TranscriptTurn",
]

from app.db.models.whatsapp import WhatsAppConnection, WhatsAppWebhookReceipt

from app.db.models.identity import User, Organization, Membership, Invitation, AuditEvent, EmailOutbox, OutboundAuthorization

# Cross-organization references are rejected independently of API filtering.
from sqlalchemy import UniqueConstraint, ForeignKeyConstraint
_tenant_models = [ImportBatch, Lead, LeadContact, ConsentEvent, ContactPreference, Conversation, CallSession, TranscriptTurn, WhatsAppConnection]
for model in _tenant_models:
    table = model.__table__
    table.append_constraint(UniqueConstraint('organization_id', 'id', name=f'uq_{table.name}_org_id'))
for model, column, parent in [(Lead, 'import_batch_id', ImportBatch), (LeadContact, 'lead_id', Lead),
    (ConsentEvent, 'contact_id', LeadContact), (ContactPreference, 'contact_id', LeadContact),
    (Conversation, 'lead_id', Lead), (Conversation, 'contact_id', LeadContact),
    (CallSession, 'conversation_id', Conversation), (TranscriptTurn, 'conversation_id', Conversation),
    (TranscriptTurn, 'call_session_id', CallSession)]:
    model.__table__.append_constraint(ForeignKeyConstraint(['organization_id', column],
        [f'app.{parent.__tablename__}.organization_id', f'app.{parent.__tablename__}.id'],
        name=f'fk_{model.__tablename__}_org_{column}'))
