from pathlib import Path

from core.application.database import DatabaseAdminService


def test_database_admin_without_token_denies_by_default():
    """Fail-closed: no token configured -> every request is denied."""
    service = DatabaseAdminService(object(), Path('/tmp'))
    assert not service.authorize(None)
    assert not service.authorize('anything')


def test_database_admin_configured_token_requires_exact_match():
    service = DatabaseAdminService(object(), Path('/tmp'), token='secret')
    assert service.authorize('secret')
    assert not service.authorize(None)
    assert not service.authorize('wrong')
