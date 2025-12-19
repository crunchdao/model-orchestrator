import os
import threading
from typing import Optional

from ruamel.yaml import YAML

from model_orchestrator.entities.model_run import ModelRun
from ...entities import ModelInfo
from ...repositories.augmented_model_info_repository import AugmentedModelInfoRepository

from ...utils.logging_utils import get_logger
from ._base import ModelStateConfig, ModelStateConfigPolling

logger = get_logger()


class ModelStateConfigYamlPolling(ModelStateConfigPolling, AugmentedModelInfoRepository):

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

        self._write_lock = threading.Lock()

    def exists(self) -> bool:
        return os.path.exists(self.file_path)

    def load_raw_yaml(self) -> dict:
        root = None
        if self.exists():
            with open(self.file_path, 'r') as file:
                yaml = YAML()
                root = yaml.load(file)

        if root is None:
            root = {}

        root.setdefault('models', [])
        return root

    def _write_raw_yaml(self, root: dict):
        with self._write_lock:
            with open(self.file_path, 'w') as file:
                yaml = YAML()
                yaml.dump(root, file)

    def append(
        self,
        id: str,
        submission_id: str,
        crunch_id: str,
        desired_state=ModelRun.DesiredStatus.RUNNING,
        *,
        cruncher_id: Optional[str] = None,
        model_name: Optional[str] = None,
        cruncher_name: Optional[str] = None
    ):
        root = self.load_raw_yaml()
        new_model = {
            "id": id,
            "submission_id": submission_id,
            "crunch_id": crunch_id,
            "desired_state": desired_state.value,
        }

        if model_name is not None:
            new_model["model_name"] = model_name

        if cruncher_name is not None:
            new_model["cruncher_name"] = cruncher_name

        if cruncher_id is not None:
            new_model["cruncher_id"] = cruncher_id

        root["models"].append(new_model)

        self._write_raw_yaml(root)

    def update(
        self,
        id: str,
        *,
        submission_id: Optional[str] = None,
        desired_state: Optional[ModelRun.DesiredStatus] = None,
        model_name: Optional[str] = None,
        cruncher_name: Optional[str] = None
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

        if model_name is not None:
            model["model_name"] = model_name

        if cruncher_name is not None:
            model["cruncher_name"] = cruncher_name

        self._write_raw_yaml(root)

    def fetch_configs(self):
        try:
            return self.load_raw_yaml()['models']
        except Exception as e:
            logger.exception("Error monitoring file", exc_info=e)
            return None

    def fetch_config(self, id: str) -> dict | None:
        """Fetches the configuration of a model by ID."""
        try:
            for model in self.load_raw_yaml().get('models', []):
                if model["id"] == id:
                    return model
        except Exception as e:
            logger.exception(f"Error fetching model configuration for ID: {id}", exc_info=e)

        logger.error(f"Model configuration with ID {id} not found.")
        return None

    def get_next_id(self) -> int:
        """Calculates the next ID based on the existing IDs in the YAML."""
        models = self.fetch_configs()
        if not models:
            return 1
        max_id = max(int(model["id"]) for model in models)
        return max_id + 1

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

    def loads(self, models_id: list[str]) -> dict[str, ModelInfo]:
        model_infos = {}
        for model in self.fetch_configs():
            if model["id"] in models_id:
                model_infos[model["id"]] = ModelInfo(model.get("model_name"), model.get("cruncher_name"))

        return model_infos

    def get_slugs(self):
        slugs = set()
        models = self.fetch_configs()
        for model in models:
            slugs.add(f"{model.get("model_name")}-{model.get('cruncher_name')}")

        return slugs
