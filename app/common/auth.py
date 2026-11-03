from datetime import datetime, timezone
from functools import wraps
from uuid import UUID

import jwt
from flask import current_app, g, request

from app.common.errors import api_error
from app.common.jwks import JwksUnavailable, UnknownSigningKey
from app.common.supabase_data import SupabaseAuthAPIError, SupabaseDataAPIError


def _error(status, code):
    return api_error(status, code)


def _bearer_token():
    value = request.headers.get("Authorization", "")
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def verify_access_token(token):
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise jwt.InvalidTokenError from None

    algorithms = current_app.config["SUPABASE_JWT_ALGORITHMS"]
    algorithm = header.get("alg")
    kid = header.get("kid")
    if algorithm not in algorithms or not isinstance(kid, str) or not kid:
        raise jwt.InvalidTokenError

    key_provider = current_app.extensions["kickoff_jwks"]
    key = key_provider.get_public_key(kid, algorithm)
    claims = jwt.decode(
        token,
        key,
        algorithms=list(algorithms),
        issuer=current_app.config["SUPABASE_JWT_ISSUER"],
        audience=current_app.config["SUPABASE_JWT_AUDIENCE"],
        leeway=current_app.config["SUPABASE_JWT_LEEWAY_SECONDS"],
        options={
            "require": ["exp", "iat", "iss", "aud", "sub", "role", "session_id"],
            "verify_signature": True,
            "verify_exp": True,
            "verify_iat": True,
            "verify_iss": True,
            "verify_aud": True,
        },
    )

    try:
        user_id = str(UUID(claims["sub"]))
        UUID(claims["session_id"])
    except (ValueError, TypeError, AttributeError, KeyError):
        raise jwt.InvalidTokenError from None

    if claims.get("role") != "authenticated" or claims.get("is_anonymous") is True:
        raise jwt.InvalidTokenError

    return claims, user_id


def _as_utc_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def require_auth(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        token = _bearer_token()
        if not token:
            return _error(401, "token_invalid")

        try:
            claims, user_id = verify_access_token(token)
        except jwt.ExpiredSignatureError:
            return _error(401, "token_expired")
        except JwksUnavailable:
            return _error(503, "auth_unavailable")
        except (jwt.PyJWTError, UnknownSigningKey, ValueError, TypeError):
            return _error(401, "token_invalid")

        data_api = current_app.extensions["kickoff_supabase_data"]
        try:
            auth_user = data_api.get_authenticated_user(token)
        except SupabaseAuthAPIError as error:
            if error.status == 404:
                return _error(401, "account_deleted")
            if error.status in (401, 403):
                # Auth soft deletion can reject the token before the 30-day
                # app-data retention period ends. Check our user-scoped,
                # tombstone-aware RPC before classifying it as a revocation.
                try:
                    deletion = data_api.rpc("get_my_account_deletion_status", token)
                except SupabaseDataAPIError:
                    deletion = None
                if isinstance(deletion, list) and len(deletion) == 1:
                    deletion = deletion[0]
                if isinstance(deletion, dict) and deletion.get("status") in ("pending", "purged"):
                    return _error(401, "account_deleted")
                return _error(401, "token_revoked")
            return _error(503, "auth_unavailable")

        if auth_user.get("id") != user_id:
            return _error(401, "token_invalid")

        try:
            profile = data_api.rpc("ensure_user_profile", token)
        except SupabaseDataAPIError as error:
            if error.status == 401:
                return _error(401, "token_invalid")
            return _error(503, "service_unavailable")

        if isinstance(profile, list) and len(profile) == 1:
            profile = profile[0]
        if not isinstance(profile, dict) or profile.get("id") != user_id:
            return _error(503, "service_unavailable")
        if profile.get("deleted_at") is not None:
            return _error(401, "account_deleted")

        issued_at = datetime.fromtimestamp(claims["iat"], tz=timezone.utc)
        valid_after = _as_utc_datetime(profile.get("tokens_valid_after"))
        if valid_after and issued_at < valid_after:
            return _error(401, "token_revoked")

        g.auth_user_id = user_id
        g.auth_access_token = token
        g.auth_profile = {
            key: profile.get(key)
            for key in (
                "id", "display_name", "language", "timezone",
                "settings", "created_at", "updated_at",
            )
        }
        # Used only for the user's requested data export. Never put this into
        # logs or error payloads.
        g.auth_email = auth_user.get("email") if isinstance(auth_user.get("email"), str) else None
        return handler(*args, **kwargs)

    return wrapped
