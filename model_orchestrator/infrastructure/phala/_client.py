"""
HTTP client for the Phala spawntee TEE service.

Provides methods to call all spawntee API endpoints:
build-model, start-model, stop-model, task status, running models, keypair.

Authentication uses the coordinator's RSA cert signature (same key that
signs gRPC calls to model containers). Each request is signed with
X-Gateway-Auth-* headers containing a timestamped payload, RSA signature,
and public key. The spawntee verifies the public key hash against on-chain
cert hashes.
"""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ...utils.logging_utils import get_logger

if TYPE_CHECKING:
    from model_runner_client.security.gateway_credentials import GatewayCredentials

logger = get_logger()

# Retry defaults for transient network errors
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 2.0  # seconds, doubled each attempt


class SpawnteeClientError(Exception):
    """Raised when a spawntee API call fails."""

    def __init__(self, message: str, status_code: int | None = None, response_body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class SpawnteeAuthenticationError(SpawnteeClientError):
    """Raised on 401/403 from spawntee — indicates a critical config error.

    This must NOT be caught and silently ignored. An authentication failure
    means the coordinator RSA key is wrong, missing, or its cert hash is not
    registered on-chain. The orchestrator cannot safely make any decisions
    (capacity, routing, provisioning).
    """
    pass


class SpawnteeClient:
    """
    HTTP client for communicating with the Phala spawntee TEE service.

    The spawntee exposes its API on a specific port (default 9010) on the CVM.
    Model gRPC services are exposed on dynamically allocated ports (50001+).

    URL pattern:
        https://<hash>-<port>.<domain>
    """

    def __init__(
        self,
        cluster_url_template: str,
        spawntee_port: int = 9010,
        timeout: int = 30,
        gateway_credentials: GatewayCredentials | None = None,
    ):
        """
        Args:
            cluster_url_template: CVM URL template with <model-port> placeholder,
                e.g. "https://abc123-<model-port>.dstack-prod4.phala.network"
            spawntee_port: Port where the spawntee API listens (default 9010)
            timeout: HTTP request timeout in seconds
            gateway_credentials: Coordinator RSA credentials for gateway auth signing.
                When set, each request is signed with X-Gateway-Auth-* headers.
        """
        self._cluster_url_template = cluster_url_template
        self._spawntee_port = spawntee_port
        self._timeout = timeout
        self._base_url = self._build_url(spawntee_port)
        self._session = requests.Session()
        self._gateway_credentials = gateway_credentials
        # Pre-compute base64-encoded DER public key (doesn't change per request)
        if gateway_credentials:
            pubkey_der = gateway_credentials.private_key.public_key().public_bytes(
                Encoding.DER, PublicFormat.SubjectPublicKeyInfo
            )
            self._pubkey_b64 = base64.b64encode(pubkey_der).decode()
        else:
            self._pubkey_b64 = ""

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

    def _build_auth_headers(self, path: str) -> dict[str, str]:
        """Sign a request with the coordinator RSA key.

        Produces the same header format as the gRPC GatewayAuthClientInterceptor:
        a JSON payload with a timestamp, signed with RSA PKCS#1v15 + SHA-256.

        Raises RuntimeError if called without gateway credentials configured.
        """
        if not self._gateway_credentials:
            raise RuntimeError(
                "_build_auth_headers called but no gateway credentials configured"
            )
        payload = json.dumps(
            {"path": path, "timestamp": int(time.time())},
            separators=(",", ":"),
        ).encode()
        signature = self._gateway_credentials.private_key.sign(
            payload, padding.PKCS1v15(), hashes.SHA256()
        )
        return {
            "X-Gateway-Auth-Message": base64.b64encode(payload).decode(),
            "X-Gateway-Auth-Signature": base64.b64encode(signature).decode(),
            "X-Gateway-Auth-Pubkey": self._pubkey_b64,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        max_retries: int = 0,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        **kwargs,
    ) -> requests.Response:
        """Make an HTTP request to the spawntee API.

        Args:
            max_retries: Number of retries on transient (network/timeout) errors.
                         0 = no retries (default for mutating calls).
            retry_backoff: Initial backoff in seconds, doubled each attempt.

        Raises:
            SpawnteeAuthenticationError: On 401/403 (never retried).
            SpawnteeClientError: On other HTTP errors or after retries exhausted.
        """
        url = f"{self._base_url}{path}"
        kwargs.setdefault("timeout", self._timeout)

        last_error: SpawnteeClientError | None = None

        for attempt in range(1 + max_retries):
            # Inject gateway auth headers per attempt (fresh timestamp each time)
            if self._gateway_credentials:
                auth_headers = self._build_auth_headers(path)
                if "headers" in kwargs:
                    kwargs["headers"].update(auth_headers)
                else:
                    kwargs["headers"] = auth_headers

            try:
                response = self._session.request(method, url, **kwargs)
            except requests.ConnectionError as e:
                last_error = SpawnteeClientError(f"Connection to spawntee failed: {e}")
                last_error.__cause__ = e
            except requests.Timeout as e:
                last_error = SpawnteeClientError(f"Spawntee request timed out: {e}")
                last_error.__cause__ = e
            except requests.RequestException as e:
                last_error = SpawnteeClientError(f"Spawntee request failed: {e}")
                last_error.__cause__ = e
            else:
                # Got an HTTP response — check status
                if response.status_code in (401, 403):
                    raise SpawnteeAuthenticationError(
                        f"Spawntee authentication failed ({response.status_code}): {response.text}. "
                        f"Check coordinator RSA key and on-chain cert hash configuration.",
                        status_code=response.status_code,
                        response_body=response.text,
                    )

                if response.status_code >= 500:
                    # Server error — retryable
                    last_error = SpawnteeClientError(
                        f"Spawntee returned {response.status_code}: {response.text}",
                        status_code=response.status_code,
                        response_body=response.text,
                    )
                elif response.status_code >= 400:
                    # Client error (4xx except auth) — not retryable
                    raise SpawnteeClientError(
                        f"Spawntee returned {response.status_code}: {response.text}",
                        status_code=response.status_code,
                        response_body=response.text,
                    )
                else:
                    return response

            # Transient error — retry if we have attempts left
            if attempt < max_retries:
                delay = retry_backoff * (2 ** attempt)
                logger.warning(
                    "⚠️ %s %s failed (attempt %d/%d): %s — retrying in %.1fs",
                    method, path, attempt + 1, 1 + max_retries, last_error, delay,
                )
                time.sleep(delay)

        raise last_error  # type: ignore[misc]

    # ── Spawntee API methods ──────────────────────────────────────────

    def health(self) -> dict:
        """GET / — health check (retries on transient errors)."""
        return self._request("GET", "/", max_retries=DEFAULT_MAX_RETRIES).json()

    def get_keypair(self, submission_id: str) -> dict:
        """GET /keypair/{submission_id} — get public key for encryption."""
        return self._request("GET", f"/keypair/{submission_id}").json()

    def build_model(self, submission_id: str, model_name: str | None = None) -> dict:
        """
        POST /build-model — submit a build task.

        Returns dict with task_id, status, message.
        """
        payload = {"submission_id": submission_id}
        if model_name:
            payload["model_name"] = model_name
        response = self._request("POST", "/build-model", json=payload)
        return response.json()

    def start_model(self, task_id: str, memory_mb: int | None = None, cpu_vcpus: int | None = None) -> dict:
        """
        POST /start-model — start a pre-built model container.

        Args:
            task_id: Spawntee task ID from a previous build.
            memory_mb: Memory limit in MB (e.g. 512). None = no limit.
            cpu_vcpus: CPU limit in vCPUs (e.g. 1). None = no limit.

        Returns dict with task_id, status, message.
        """
        payload: dict = {"task_id": task_id}
        if memory_mb is not None:
            payload["memory_mb"] = memory_mb
        if cpu_vcpus is not None:
            payload["cpu_vcpus"] = cpu_vcpus
        response = self._request("POST", "/start-model", json=payload)
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
        return self._request("GET", f"/task/{task_id}", max_retries=DEFAULT_MAX_RETRIES).json()

    def get_running_models(self) -> list[dict]:
        """
        GET /running_models — list all running model containers.

        Returns list of running model dicts with task_id, container_id, status, port.
        """
        response = self._request("GET", "/running_models", max_retries=DEFAULT_MAX_RETRIES).json()
        return response.get("running_models", [])

    def check_model_image(self, submission_id: str) -> dict:
        """
        GET /submission-image/{submission_id} — check if model image exists.

        Returns dict with submission_id, image_exists, image_name.
        """
        return self._request("GET", f"/submission-image/{submission_id}", max_retries=DEFAULT_MAX_RETRIES).json()

    def capacity(self) -> dict:
        """
        GET /capacity — get CVM resource capacity.

        Returns dict with total_memory_mb, available_memory_mb, running_models,
        accepting_new_models, capacity_threshold, etc.
        """
        return self._request("GET", "/capacity", max_retries=DEFAULT_MAX_RETRIES).json()

    def has_capacity(self) -> bool:
        """
        Check if this CVM is accepting new models.

        Retries on transient errors (network, timeout, 5xx) before giving up.
        Raises on auth errors (401/403) and after retries are exhausted —
        the caller must not make capacity decisions without a real answer.
        """
        return self.capacity().get("accepting_new_models", False)

    def get_builder_logs(
        self, task_id: str, follow: bool = False, tail: int = 0, from_start: bool = True,
        stream: bool = False,
    ) -> requests.Response:
        """
        GET /logs/builder/{task_id} — fetch builder logs from spawntee.

        Args:
            stream: If True, returns a streaming Response (caller must iterate
                    response.iter_lines() and close the response when done).

        Returns the raw Response so the caller can stream or read the body.
        """
        params = {"follow": str(follow).lower(), "tail": str(tail), "from_start": str(from_start).lower()}
        return self._request(
            "GET", f"/logs/builder/{task_id}",
            params=params,
            max_retries=DEFAULT_MAX_RETRIES if not stream else 0,
            stream=stream,
        )

    def get_runner_logs(
        self, task_id: str, follow: bool = False, tail: int = 200, from_start: bool = False,
        stream: bool = False,
    ) -> requests.Response:
        """
        GET /logs/runner/{task_id} — fetch runner logs from spawntee.

        Args:
            stream: If True, returns a streaming Response (caller must iterate
                    response.iter_lines() and close the response when done).

        Returns the raw Response so the caller can stream or read the body.
        """
        params = {"follow": str(follow).lower(), "tail": str(tail), "from_start": str(from_start).lower()}
        return self._request(
            "GET", f"/logs/runner/{task_id}",
            params=params,
            max_retries=DEFAULT_MAX_RETRIES if not stream else 0,
            stream=stream,
        )
