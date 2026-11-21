import json
import re
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class SupersetError(RuntimeError):
    def __init__(self, operation, status, message):
        self.operation = operation
        self.status = status
        super().__init__(f"{operation} failed ({status}): {message}")


class SupersetClient:
    def __init__(self, base_url, username=None, password=None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.headers = {}
        self.csrf = None

    def authenticate(self):
        payload = self._request(
            "POST",
            "/api/v1/security/login",
            {
                "username": self.username,
                "password": self.password,
                "provider": "db",
                "refresh": False,
            },
            {200},
            "login",
            csrf=False,
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SupersetError("login", 502, "access token missing")
        self.headers["Authorization"] = f"Bearer {token}"
        payload = self._request(
            "GET",
            "/api/v1/security/csrf_token/",
            None,
            {200},
            "csrf",
            csrf=False,
        )
        self.csrf = payload.get("result")
        if not isinstance(self.csrf, str) or not self.csrf:
            raise SupersetError("csrf", 502, "token missing")
        return self

    def _request(
        self,
        method,
        path,
        body,
        expected,
        operation,
        csrf=True,
    ):
        raw = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json", **self.headers}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        if csrf and method in {"POST", "PUT", "DELETE"}:
            headers["X-CSRFToken"] = self.csrf or ""
            headers["Referer"] = f"{self.base_url}/"
        request = Request(
            self.base_url + path,
            data=raw,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                status = response.status
                payload = self._decode(response.read(MAX_RESPONSE_BYTES + 1))
        except HTTPError as error:
            status = error.code
            payload = self._decode(error.read(MAX_RESPONSE_BYTES + 1))
        except (TimeoutError, URLError) as error:
            raise SupersetError(operation, 503, type(error).__name__) from None
        if status not in expected:
            raise SupersetError(
                operation,
                status,
                self._message(payload),
            )
        return payload

    def _decode(self, raw):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SupersetError("response", 502, "body exceeds 2 MiB")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise SupersetError("response", 502, "invalid JSON") from None
        if not isinstance(value, dict):
            raise SupersetError("response", 502, "object expected")
        return value

    def _message(self, payload):
        message = payload.get("message", "upstream rejected request")
        if not isinstance(message, str):
            message = json.dumps(message, separators=(",", ":"))
        if self.password:
            message = message.replace(self.password, "***")
        return re.sub(r"(?i)(password|passwd)=([^&\s]+)", r"\1=***", message)[:500]

    def test_database(self, payload):
        allowed = {
            "engine",
            "driver",
            "parameters",
            "configuration_method",
            "database_name",
            "impersonate_user",
            "extra",
            "masked_encrypted_extra",
            "server_cert",
            "sqlalchemy_uri",
            "ssh_tunnel",
        }
        return self._request(
            "POST",
            "/api/v1/database/test_connection/",
            {key: value for key, value in payload.items() if key in allowed},
            {200},
            "database test",
        )

    def create_database(self, payload):
        return self._request(
            "POST",
            "/api/v1/database/",
            payload,
            {201},
            "database create",
        )

    def update_database(self, database_id, payload):
        return self._request(
            "PUT",
            f"/api/v1/database/{database_id}",
            payload,
            {200},
            "database update",
        )

    def delete_database(self, database_id):
        return self._request(
            "DELETE",
            f"/api/v1/database/{database_id}",
            None,
            {200},
            "database delete",
        )

    def schemas(self, database_id):
        payload = self._request(
            "GET",
            f"/api/v1/database/{database_id}/schemas/",
            None,
            {200},
            "schemas",
        )
        return payload.get("result", [])

    def tables(self, database_id, schema):
        query = quote(f"(schema_name:'{schema}')", safe="():,'")
        payload = self._request(
            "GET",
            f"/api/v1/database/{database_id}/tables/?q={query}",
            None,
            {200},
            "tables",
        )
        return payload.get("result", [])

    def create_dataset(self, database_id, schema, table):
        return self._request(
            "POST",
            "/api/v1/dataset/",
            {"database": database_id, "schema": schema, "table_name": table},
            {201},
            "dataset create",
        )

    def get_dataset(self, dataset_id):
        payload = self._request(
            "GET",
            f"/api/v1/dataset/{dataset_id}",
            None,
            {200},
            "dataset get",
        )
        return payload.get("result", {})

    def delete_dataset(self, dataset_id):
        return self._request(
            "DELETE",
            f"/api/v1/dataset/{dataset_id}",
            None,
            {200},
            "dataset delete",
        )

    def role(self, name):
        query = quote("(page:0,page_size:1000)", safe="():,")
        payload = self._request(
            "GET",
            f"/api/v1/security/roles/search/?q={query}",
            None,
            {200},
            "role lookup",
        )
        return next(
            (item for item in payload.get("result", []) if item.get("name") == name),
            None,
        )

    def permissions(self):
        result = []
        page = 0
        while True:
            query = quote(f"(page:{page},page_size:100)", safe="():,")
            payload = self._request(
                "GET",
                f"/api/v1/security/permissions-resources/?q={query}",
                None,
                {200},
                "permission lookup",
            )
            batch = payload.get("result", [])
            result.extend(batch)
            if len(result) >= payload.get("count", len(result)) or not batch:
                return result
            page += 1

    def grant_permission(self, role_name, permission_name, resource_name):
        role = self.role(role_name)
        if role is None:
            raise SupersetError("role lookup", 404, "role missing")
        permission = next(
            (
                item
                for item in self.permissions()
                if item.get("permission", {}).get("name") == permission_name
                and item.get("view_menu", {}).get("name") == resource_name
            ),
            None,
        )
        if permission is None:
            raise SupersetError("permission lookup", 404, "resource missing")
        permission_ids = sorted(
            set(role.get("permission_ids", [])) | {permission["id"]}
        )
        self._request(
            "POST",
            f"/api/v1/security/roles/{role['id']}/permissions",
            {"permission_view_menu_ids": permission_ids},
            {200},
            "role permission update",
        )

    def grant_database(self, role_name, database_name, database_id):
        self.grant_permission(
            role_name,
            "database_access",
            f"[{database_name}].(id:{database_id})",
        )

    def grant_dataset(self, role_name, database_name, table_name, dataset_id):
        self.grant_permission(
            role_name,
            "datasource_access",
            f"[{database_name}].[{table_name}](id:{dataset_id})",
        )

    def chart_data(self, payload):
        return self._request(
            "POST",
            "/api/v1/chart/data",
            payload,
            {200},
            "dataset query",
        )
