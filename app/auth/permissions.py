"""The database role is authoritative; Firebase claims are a versioned snapshot."""
ROLES = {
    'viewer': frozenset({'leads.read'}),
    'member': frozenset({'leads.read', 'leads.import', 'calls.create'}),
    'org_admin': frozenset({'leads.read', 'leads.import', 'calls.create', 'members.manage', 'settings.manage', 'integrations.manage', 'audit.read'}),
}
PLATFORM_PERMISSIONS = ['organizations.manage']


def claims_for_user(user):
    """Shared compact claims payload for the bootstrap CLI and background worker."""
    is_admin = user.platform_admin and user.active
    return {
        'platform_role': 'admin' if is_admin else 'user',
        'permissions': PLATFORM_PERMISSIONS if is_admin else [],
        'permissions_version': user.permissions_version,
    }
