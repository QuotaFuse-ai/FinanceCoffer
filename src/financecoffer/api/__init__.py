"""REST surface (``financecoffer.md`` section 6.2).

``create_app`` builds the ASGI application; the request/response model and the
error/HTTP mapping live in :mod:`financecoffer.api.app`.
"""

from financecoffer.api.app import AnnouncementSearchRequest, create_app

__all__ = ["AnnouncementSearchRequest", "create_app"]
