# test_model_state_config_polling.py

import threading
import unittest
from unittest.mock import MagicMock

from model_orchestrator.infrastructure.config_watcher import (
    ModelStateConfigOnChainPolling, ModelStateConfigPolling)


class TestModelStateConfigPolling(unittest.TestCase):

    def setUp(self):
        self.mock_callback = MagicMock()
        self.stop_event = threading.Event()
        self.polling_instance = ModelStateConfigOnChainPolling(
            url=None,
            crunch_names={"bird-game"},
            on_config_change_callback=self.mock_callback,
            stop_event=self.stop_event,
            interval=2
        )
        self.polling_instance.fetch_configs = MagicMock()

    def test_compare_configs_no_differences(self):
        old_configs = [{"id": 1, "name": "TestConfig"}]
        new_configs = [{"id": 1, "name": "TestConfig"}]
        changes = self.polling_instance.compare_configs(old_configs, new_configs)
        self.assertIsNone(changes)

    def test_compare_configs_with_differences(self):
        old_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "2025011402",
                    "resourceId": "",
                    "id": "2025011402"
                }
            }
        ]
        new_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "123",
                    "resourceId": "",
                    "id": "2025011402"
                }
            }
        ]
        changes = self.polling_instance.compare_configs(old_configs, new_configs)
        self.assertListEqual(changes, new_configs)

    def test_compare_configs_with_differences_deep(self):
        old_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "2025011402",
                    "resourceId": "",
                    "id": "2025011402"
                }
            }
        ]
        new_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "gpu": {}
                    },
                    "submissionId": "123",
                    "resourceId": "",
                    "id": "2025011402"
                }
            }
        ]
        changes = self.polling_instance.compare_configs(old_configs, new_configs)
        self.assertListEqual(changes, new_configs)

    def test_compare_configs_with_differences_new(self):
        old_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "2025011402",
                    "resourceId": "",
                    "id": "2025011402"
                }
            }
        ]
        new_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "2025011402",
                    "resourceId": "",
                    "id": "2025011402"
                }
            },
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bkl",
                    "hardwareType": {
                        "gpu": {}
                    },
                    "submissionId": "145",
                    "resourceId": "",
                    "id": "2025011403"
                }
            }
        ]
        changes = self.polling_instance.compare_configs(old_configs, new_configs)
        self.assertListEqual(changes, [new_configs[1]])

    def test_compare_configs_with_differences_del(self):
        old_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "2025011402",
                    "resourceId": "",
                    "id": "2025011402"
                }
            },
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bkl",
                    "hardwareType": {
                        "gpu": {}
                    },
                    "submissionId": "145",
                    "resourceId": "",
                    "id": "2025011403"
                }
            }
        ]
        new_configs = [
            {
                "desiredState": {
                    "start": {}
                },
                "model": {
                    "bump": 255,
                    "initialized": True,
                    "owner": "AkAbNijbAQ3deAcMg4CUSjnTY9B1nSAbg58bMmBx5bzQ",
                    "hardwareType": {
                        "cpu": {}
                    },
                    "submissionId": "2025011402",
                    "resourceId": "",
                    "id": "2025011402"
                }
            }
        ]
        changes = self.polling_instance.compare_configs(old_configs, new_configs)
        self.assertIsNone(changes)

    def test_abstract_methods_exist(self):
        with self.assertRaises(TypeError):
            ModelStateConfigPolling(
                crunch_names={"bird-game"},
                on_config_change_callback=self.mock_callback,
                stop_event=self.stop_event,
                interval=1
            )


if __name__ == "__main__":
    unittest.main()
