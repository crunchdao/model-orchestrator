import requests
from pydantic.v1.utils import to_lower_camel

from ...utils.logging_utils import get_logger
from ._base import ModelStateConfig, ModelStateConfigPolling

logger = get_logger()


class ModelStateConfigOnChain(ModelStateConfig):
    def __init__(
        self,
        crunch_address: str,
        cruncher_wallet_pubkey: str,
        cruncher_hotkey: str,
        coordinator_wallet_pubkey: str,
        coordinator_cert_hash: str,
        coordinator_cert_hash_secondary: str,
        **kwargs
    ):
        self.crunch_address = crunch_address
        self.cruncher_wallet_pubkey = cruncher_wallet_pubkey
        self.cruncher_hotkey = cruncher_hotkey
        self.coordinator_wallet_pubkey = coordinator_wallet_pubkey
        self.coordinator_cert_hash = coordinator_cert_hash
        self.coordinator_cert_hash_secondary = coordinator_cert_hash_secondary

        super().__init__(**kwargs)

    def __str__(self) -> str:
        base = super().__str__()
        extra = (
            f"crunch_address={self.crunch_address}, "
            f"cruncher_wallet_pubkey={self.cruncher_wallet_pubkey}, "
            f"cruncher_hotkey={self.cruncher_hotkey}, "
            f"coordinator_wallet_pubkey={self.coordinator_wallet_pubkey}, "
            f"coordinator_cert_hash={self.coordinator_cert_hash}, "
            f"coordinator_cert_hash_secondary={self.coordinator_cert_hash_secondary}"
        )

        if base.endswith(")"):
            return base[:-1] + ", " + extra + ")"
        return base + " " + extra


class ModelStateConfigOnChainPolling(ModelStateConfigPolling):

    def __init__(
        self,
        *,
        url,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.url = url
        self.params = {
            "crunchNames[]": list(self.crunch_names),
        }

    def fetch_configs(self):
        try:
            response = requests.get(
                f'{self.url.rstrip("/")}/models/states',
                params=self.params
            )

            response.raise_for_status()  # Raises an HTTP exception in case of error
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Error retrieving configurations: {e}")
            return None

    def create_models_state(self, config_entries) -> list[ModelStateConfig]:
        return [
            ModelStateConfigOnChain(
                crunch_address=config_entry["crunchPubKey"],
                cruncher_wallet_pubkey=config_entry["cruncherWalletPubkey"],
                cruncher_hotkey=config_entry["cruncherSmpHotkey"],
                coordinator_wallet_pubkey=config_entry["coordinatorWalletPubkey"],
                coordinator_cert_hash=(config_entry.get("coordinatorCertificate") or {}).get("certHash", ""),
                coordinator_cert_hash_secondary=(config_entry.get("coordinatorCertificate") or {}).get("certHashSecondary", ""),
                id=config_entry["model"]["id"],
                name="",  # config_entry["model"]["modelName"],
                submission_id=config_entry["model"]["submissionId"],
                resource_id=config_entry["model"].get("resourceId", ""),
                hardware_type="CPU" if "cpu" in to_lower_camel(config_entry["model"]["hardwareType"]) else "GPU",
                crunch_id=config_entry["crunchName"],
                cruncher_id=config_entry["model"].get("owner", ""),
                signature=config_entry["model"].get("signature", ""),
                desired_state="RUNNING" if "start" in to_lower_camel(config_entry["desiredState"]) else "STOPPED"
            )
            for config_entry in config_entries
        ]
