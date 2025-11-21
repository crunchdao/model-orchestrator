import os
from typing import Optional

from model_orchestrator.entities.model_run import ModelRun

from ...utils.logging_utils import get_logger
from ._base import ModelStateConfig, ModelStateConfigPolling

logger = get_logger()


class ModelStateConfigYamlPolling(ModelStateConfigPolling):

    def __init__(
        self,
        *,
        file_path,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.file_path = file_path
        if not self.exists():
            logger.error(f"File not found: {file_path}")

        from ruamel.yaml import YAML
        self._yaml = YAML()

    def exists(self) -> bool:
        return os.path.exists(self.file_path)

    def load_raw_yaml(self) -> dict:
        root = None
        if self.exists():
            with open(self.file_path, 'r') as file:
                root = self._yaml.load(file)

        if root is None:
            root = {}

        root.setdefault('models', [])

        return root

    def _write_raw_yaml(self, root: dict):
        with open(self.file_path, 'w') as file:
            self._yaml.dump(root, file)

    def append(
        self,
        id: str,
        name: str,
        submission_id: str,
        crunch_id: str,
        desired_state=ModelRun.DesiredStatus.RUNNING,
    ):
        root = self.load_raw_yaml()
        root["models"].append({
            "id": id,
            "name": name,
            "submission_id": submission_id,
            "crunch_id": crunch_id,
            "desired_state": desired_state.value,
        })

        self._write_raw_yaml(root)

    def update(
        self,
        id: str,
        *,
        submission_id: Optional[str] = None,
        desired_state: Optional[ModelRun.DesiredStatus] = None,
    ):
        root = self.load_raw_yaml()

        for model in root["models"]:
            if model["id"] == id:
                break
        else:
            raise ValueError(f"Model state with id {id} not found in the YAML file.")

        if submission_id is not None:
            model["submission_id"] = submission_id

        if desired_state is not None:
            model["desired_state"] = desired_state.value

        self._write_raw_yaml(root)

    def fetch_configs(self):
        try:
            return self.load_raw_yaml()['models']
        except Exception as e:
            logger.exception("Error monitoring file", exc_info=e)
            return None

    def create_models_state(self, config_entries):
        return [
            ModelStateConfig(
                id=config_entry.get("id"),
                # name=config_entry["model"]["modelName"],
                name=config_entry.get("name"),
                submission_id=config_entry.get("submission_id"),
                resource_id=config_entry.get("resource_id"),
                hardware_type=config_entry.get("hardware_type"),
                crunch_id=config_entry.get("crunch_id"),
                cruncher_id=config_entry.get("cruncher_id"),
                desired_state=config_entry.get("desired_state"),
                signature=config_entry.get("signature", "")
            )
            for config_entry in config_entries
        ]
