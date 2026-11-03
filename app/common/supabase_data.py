import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class SupabaseDataAPIError(Exception):
    def __init__(self, status=None):
        super().__init__("Supabase Data API request failed")
        self.status = status


class SupabaseAuthAPIError(Exception):
    def __init__(self, status=None):
        super().__init__("Supabase Auth verification request failed")
        self.status = status


class SupabaseAuthAdminError(Exception):
    def __init__(self, status=None):
        super().__init__("Supabase Auth admin request failed")
        self.status = status


class SupabaseDataAPI:
    def __init__(self, project_url, publishable_key, timeout_seconds=5):
        self.project_url = project_url.rstrip("/")
        self.publishable_key = publishable_key
        self.timeout_seconds = timeout_seconds

    def get_authenticated_user(self, access_token):
        request = Request(
            f"{self.project_url}/auth/v1/user",
            headers={
                "apikey": self.publishable_key,
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(65537)
        except HTTPError as error:
            raise SupabaseAuthAPIError(status=error.code) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseAuthAPIError from None

        if len(payload) > 65536:
            raise SupabaseAuthAPIError
        try:
            user = json.loads(payload)
        except (TypeError, ValueError):
            raise SupabaseAuthAPIError from None
        if not isinstance(user, dict):
            raise SupabaseAuthAPIError
        return user

    def rpc(self, function_name, access_token, parameters=None, *, max_response_bytes=1048576):
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1024 <= max_response_bytes <= 8388608
        ):
            raise SupabaseDataAPIError
        safe_name = quote(function_name, safe="")
        url = f"{self.project_url}/rest/v1/rpc/{safe_name}"
        body = json.dumps(parameters or {}, separators=(",", ":")).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "apikey": self.publishable_key,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(max_response_bytes + 1)
        except HTTPError as error:
            raise SupabaseDataAPIError(status=error.code) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseDataAPIError from None

        if len(payload) > max_response_bytes:
            raise SupabaseDataAPIError(status=413)
        try:
            return json.loads(payload) if payload else None
        except (TypeError, ValueError):
            raise SupabaseDataAPIError from None

    def rpc_server(self, function_name, secret_key, parameters=None):
        """Call a narrowly granted server-only RPC with the server API key.

        New `sb_secret_` keys are API keys and belong only in `apikey`. Legacy
        service_role keys are JWTs and also need the Bearer header. Failures
        intentionally omit response bodies, keys, and RPC arguments.
        """
        if not isinstance(secret_key, str) or not secret_key:
            raise SupabaseDataAPIError
        safe_name = quote(function_name, safe="")
        url = f"{self.project_url}/rest/v1/rpc/{safe_name}"
        body = json.dumps(parameters or {}, separators=(",", ":")).encode("utf-8")
        headers = {
            "apikey": secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if secret_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {secret_key}"
        request = Request(url, data=body, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(65537)
        except HTTPError as error:
            raise SupabaseDataAPIError(status=error.code) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseDataAPIError from None
        if len(payload) > 65536:
            raise SupabaseDataAPIError
        try:
            return json.loads(payload) if payload else None
        except (TypeError, ValueError):
            raise SupabaseDataAPIError from None

    def delete_auth_user(self, user_id, secret_key, *, should_soft_delete):
        """Delete/soft-delete a Supabase Auth user using a server-only key."""
        if not isinstance(secret_key, str) or not secret_key:
            raise SupabaseAuthAdminError
        safe_user_id = quote(str(user_id), safe="")
        body = json.dumps({"should_soft_delete": bool(should_soft_delete)}, separators=(",", ":")).encode("utf-8")
        headers = {
            "apikey": secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if secret_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {secret_key}"
        request = Request(
            f"{self.project_url}/auth/v1/admin/users/{safe_user_id}",
            data=body,
            method="DELETE",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response.read(4096)
        except HTTPError as error:
            raise SupabaseAuthAdminError(status=error.code) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseAuthAdminError from None

    def select_public_templates(self):
        """Read active public templates through the anon Data API role."""
        columns = (
            "id,template_key,task_type,language,version,title,description,"
            "keywords,variants,is_active,updated_at"
        )
        query = urlencode({
            "select": columns,
            "is_active": "eq.true",
            "order": "task_type.asc,language.asc,template_key.asc,version.desc,id.asc",
            "limit": "1000",
        })
        request = Request(
            f"{self.project_url}/rest/v1/templates?{query}",
            headers={
                "apikey": self.publishable_key,
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(1048577)
        except HTTPError as error:
            raise SupabaseDataAPIError(status=error.code) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseDataAPIError from None

        if len(payload) > 1048576:
            raise SupabaseDataAPIError
        try:
            rows = json.loads(payload) if payload else []
        except (TypeError, ValueError):
            raise SupabaseDataAPIError from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SupabaseDataAPIError
        return rows
