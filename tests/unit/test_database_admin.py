from pathlib import Path

from core.application.database import DatabaseAdminService


def test_database_admin_without_token_uses_page_authentication():
    service = DatabaseAdminService(object(), Path('/tmp'))
    assert service.authorize(None)
    assert service.authorize('anything')


def test_database_admin_configured_token_requires_exact_match():
    service = DatabaseAdminService(object(), Path('/tmp'), token='secret')
    assert service.authorize('secret')
    assert not service.authorize(None)
    assert not service.authorize('wrong')
