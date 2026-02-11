"""
HTTP client for the Phala spawntee TEE service.

Provides methods to call all spawntee API endpoints:
build-model, start-model, stop-model, task status, running models, keypair.
"""

from urllib.parse import urlparse

import requests

from ...utils.logging_utils import get_logger

logger = get_logger()


class SpawnteeClientError(Exception):
    """Raised when a spawntee API call fails."""

    def __init__(self, message: str, status_code: int | None = None, response_body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class SpawnteeClient:
    """
    HTTP client for communicating with the Phala spawntee TEE service.

    The spawntee exposes its API on a specific port (default 9010) on the CVM.
    Model gRPC services are exposed on dynamically allocated ports (50001+).

    URL pattern:
        https://<hash>-<port>.<domain>
    """

    def __init__(self, cluster_url_template: str, spawntee_port: int = 9010, timeout: int = 30):
        """
        Args:
            cluster_url_template: CVM URL template with <model-port> placeholder,
                e.g. "https://abc123-<model-port>.dstack-prod4.phala.network"
            spawntee_port: Port where the spawntee API listens (default 9010)
            timeout: HTTP request timeout in seconds
        """
        self._cluster_url_template = cluster_url_template
        self._spawntee_port = spawntee_port
        self._timeout = timeout
        self._base_url = self._build_url(spawntee_port)
        self._session = requests.Session()

    def _build_url(self, port: int) -> str:
        """Build a full URL by substituting the port into the template."""
        return self._cluster_url_template.replace("<model-port>", str(port))

    def model_grpc_url(self, port: int) -> tuple[str, int]:
        """
        Construct the external hostname and port for a model's gRPC service.

        The CVM exposes each model on a unique port. The external URL follows
        the same pattern as the spawntee URL but with the model's port.

        Args:
            port: The externally-mapped gRPC port (e.g. 50001)

        Returns:
            Tuple of (hostname, port) for gRPC connection.
            hostname is the full URL with the port baked into the subdomain.
        """
        url = self._build_url(port)
        parsed = urlparse(url)
        return parsed.hostname, 443

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an HTTP request to the spawntee API."""
        url = f"{self._base_url}{path}"
        kwargs.setdefault("timeout", self._timeout)

        try:
            response = self._session.request(method, url, **kwargs)
        except requests.ConnectionError as e:
            raise SpawnteeClientError(f"Connection to spawntee failed: {e}") from e
        except requests.Timeout as e:
            raise SpawnteeClientError(f"Spawntee request timed out: {e}") from e
        except requests.RequestException as e:
            raise SpawnteeClientError(f"Spawntee request failed: {e}") from e

        if response.status_code >= 400:
            raise SpawnteeClientError(
                f"Spawntee returned {response.status_code}: {response.text}",
                status_code=response.status_code,
                response_body=response.text,
            )

        return response

    # ── Spawntee API methods ──────────────────────────────────────────

    def health(self) -> dict:
        """GET / — health check."""
        return self._request("GET", "/").json()

    def get_keypair(self, submission_id: str) -> dict:
        """GET /keypair/{submission_id} — get public key for encryption."""
        return self._request("GET", f"/keypair/{submission_id}").json()

    def build_model(self, submission_id: str) -> dict:
        """
        POST /build-model — submit a build task.

        Returns dict with task_id, status, message.
        """
        response = self._request("POST", "/build-model", json={"submission_id": submission_id})
        return response.json()

    def start_model(self, task_id: str) -> dict:
        """
        POST /start-model — start a pre-built model container.

        Returns dict with task_id, status, message.
        """
        response = self._request("POST", "/start-model", json={"task_id": task_id})
        return response.json()

    def stop_model(self, task_id: str) -> dict:
        """
        POST /stop-model — stop a running model container.

        Returns dict with task_id, status, message.
        """
        response = self._request("POST", "/stop-model", json={"task_id": task_id})
        return response.json()

    def get_task(self, task_id: str) -> dict:
        """
        GET /task/{task_id} — get task status.

        Returns full task status including operations history and metadata.
        """
        return self._request("GET", f"/task/{task_id}").json()

    def get_running_models(self) -> list[dict]:
        """
        GET /running_models — list all running model containers.

        Returns list of running model dicts with task_id, container_id, status, port.
        """
        response = self._request("GET", "/running_models").json()
        return response.get("running_models", [])

    def check_model_image(self, submission_id: str) -> dict:
        """
        GET /submission-image/{submission_id} — check if model image exists.

        Returns dict with submission_id, image_exists, image_name.
        """
        return self._request("GET", f"/submission-image/{submission_id}").json()
