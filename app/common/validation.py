import re
from uuid import UUID

from flask import g, request
from werkzeug.exceptions import BadRequest


_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class RequestValidationError(Exception):
    def __init__(self, fields):
        super().__init__("Request validation failed")
        self.fields = fields


class MissingAuthContext(Exception):
    pass


def parse_json_object(*, required=True):
    """Return a JSON object from the current request, with safe field errors."""
    if not request.is_json:
        if not required and request.content_length in (None, 0):
            return None
        raise RequestValidationError([
            {"field": "Content-Type", "code": "application_json_required"}
        ])

    try:
        value = request.get_json(silent=False, cache=True)
    except BadRequest:
        raise RequestValidationError([
            {"field": "body", "code": "invalid_json"}
        ]) from None

    if not isinstance(value, dict):
        raise RequestValidationError([
            {"field": "body", "code": "object_required"}
        ])
    return value


def require_only_fields(payload, allowed):
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise RequestValidationError([
            {"field": field, "code": "unknown_field"} for field in unknown
        ])


def string_field(payload, field, *, minimum=1, maximum=200, optional=False):
    value = payload.get(field)
    if optional and field not in payload:
        return None
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise RequestValidationError([
            {"field": field, "code": "invalid_value"}
        ])
    return value


def integer_field(payload, field, *, minimum, maximum, optional=False):
    value = payload.get(field)
    if optional and field not in payload:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise RequestValidationError([
            {"field": field, "code": "invalid_value"}
        ])
    return value


def uuid_field(value, *, field="id"):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise RequestValidationError([
            {"field": field, "code": "invalid_uuid"}
        ]) from None


def authenticated_user_id():
    """Read the verified subject installed by require_auth; never accept an input ID."""
    value = getattr(g, "auth_user_id", None)
    if not value:
        raise MissingAuthContext
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise MissingAuthContext from None


def user_scope_filter(owner_column="user_id"):
    """Build a PostgREST equality filter from the verified subject only."""
    if not isinstance(owner_column, str) or not _IDENTIFIER.fullmatch(owner_column):
        raise ValueError("Invalid owner column")
    return {owner_column: f"eq.{authenticated_user_id()}"}
