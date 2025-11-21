import requests

from ...infrastructure.config_watcher._base import ModelStateConfigPolling
from ...utils.logging_utils import get_logger
from ._base import ModelStateConfig, ModelStateConfigPolling

logger = get_logger()


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
            ModelStateConfig(
                id=config_entry["model"]["id"],
                name="",  # config_entry["model"]["modelName"],
                submission_id=config_entry["model"]["submissionId"],
                resource_id=config_entry["model"].get("resourceId", ""),
                hardware_type="CPU" if "cpu" in config_entry["model"]["hardwareType"] else "GPU",
                crunch_id=config_entry["crunchName"],
                cruncher_id=config_entry["model"].get("owner", ""),
                signature=config_entry["model"].get("signature", ""),
                desired_state="RUNNING" if "start" in config_entry["desiredState"] else "STOPPED"
            )
            for config_entry in config_entries
        ]
