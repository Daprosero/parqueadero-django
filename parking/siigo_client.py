# parking/siigo_client.py
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta


class SiigoAuthError(Exception):
    """Raised when authentication fails."""
    pass


class SiigoAPIError(Exception):
    """Raised when an API call fails."""
    def __init__(self, message: str, status_code: int = None, response: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class SiigoClient:
    """Client for interacting with Siigo's REST API."""

    BASE_URL = "https://api.siigo.com"
    AUTH_ENDPOINT = "/auth"
    INVOICES_ENDPOINT = "/v1/invoices"

    def __init__(self, username: str, access_key: str, application_name: str = "MyApp"):
        self.username = username
        self.access_key = access_key
        self.application_name = application_name
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    def _get_auth_headers(self) -> Dict[str, str]:
        if self._needs_token_refresh():
            self._authenticate()

        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Partner-Id": self.application_name,
        }

    def _needs_token_refresh(self) -> bool:
        if not self._access_token or not self._token_expiry:
            return True
        return datetime.now() >= self._token_expiry - timedelta(hours=1)

    def _authenticate(self) -> None:
        url = f"{self.BASE_URL}{self.AUTH_ENDPOINT}"
        payload = {"username": self.username, "access_key": self.access_key}
        headers = {"Content-Type": "application/json", "Partner-Id": self.application_name}

        try:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            self._access_token = data.get("access_token")
            self._token_expiry = datetime.now() + timedelta(hours=24)

        except requests.exceptions.HTTPError as e:
            raise SiigoAuthError(
                f"Authentication failed: {e.response.status_code} - {e.response.text}"
            )
        except requests.exceptions.RequestException as e:
            raise SiigoAuthError(f"Connection error during authentication: {str(e)}")

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None, params: Optional[Dict] = None) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{endpoint}"
        headers = self._get_auth_headers()

        try:
            response = requests.request(method=method, url=url, headers=headers, json=data, params=params)
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}

        except requests.exceptions.HTTPError as e:
            error_response = {}
            try:
                error_response = e.response.json()
            except Exception:
                pass
            raise SiigoAPIError(
                f"API request failed: {e.response.status_code} - {e.response.text}",
                status_code=e.response.status_code,
                response=error_response
            )
        except requests.exceptions.RequestException as e:
            raise SiigoAPIError(f"Connection error: {str(e)}")

    def create_invoice(self, invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        return self._make_request("POST", self.INVOICES_ENDPOINT, data=invoice_data)

    # (si necesitas los demás métodos, déjalos aquí igual que los tenías)
