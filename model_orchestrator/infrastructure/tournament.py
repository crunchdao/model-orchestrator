from datetime import datetime, timedelta

import requests

from model_orchestrator.entities import ModelInfo
from model_orchestrator.repositories.augmented_model_info_repository import \
    AugmentedModelInfoRepository
from model_orchestrator.utils.logging_utils import get_logger

from ..utils.compat import batched


class TournamentApi(AugmentedModelInfoRepository):
    CHUNK_SIZE = 100

    def __init__(self, url: str):
        self.url = url

        self.user_mapping = {}
        self.refresh_user_mapping_interval_seconds = 300  # 5 minutes in seconds
        self.last_user_mapping_refresh = None

    def fetch_user_mapping(self):
        url = f"{self.url}/v2/users/mapping"
        get_logger().trace(f"Fetching user mapping from {url}")
        response = requests.get(url)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch data from API: {response.status_code} - {response.text}. URL: {url}")
        self.user_mapping = response.json()

    def loads(self, model_ids: list[str]) -> dict[str, ModelInfo]:
        if self._should_refresh_user_mapping():
            self.fetch_user_mapping()
        url = f"{self.url}/v4/projects/~"
        result = {}
        for ids in batched(model_ids, self.CHUNK_SIZE):
            get_logger().trace(f"Fetching user mapping from {url} for models %s", model_ids)
            response = requests.post(url, json=ids)
            if response.status_code != 200:
                raise Exception(f"Failed to fetch data from API: {response.status_code} - {response.text}. URL: {url}")

            projects = response.json()
            for project in projects:
                model_id = str(project["id"])
                if model_id in model_ids:
                    result[model_id] = ModelInfo(
                        name=project["name"],
                        cruncher_name=self.user_mapping.get(str(project["userId"]))
                    )

        return result

    def _should_refresh_user_mapping(self) -> bool:
        """Determines if user mapping should be refreshed based on the interval."""
        if self.last_user_mapping_refresh is None:
            self.last_user_mapping_refresh = datetime.now()  # Initialize on first call
            return True

        next_refresh_time = self.last_user_mapping_refresh + timedelta(seconds=self.refresh_user_mapping_interval_seconds)
        if datetime.now() >= next_refresh_time:
            self.last_user_mapping_refresh = datetime.now()
            return True

        return False


if __name__ == "__main__":
    api = TournamentApi("https://api.hub.crunchdao.io/")
    print(api.loads(["9647"]))
