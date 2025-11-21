import pytest
from unittest.mock import MagicMock

from model_orchestrator.entities import ModelRun
from model_orchestrator.entities.crunch import ScheduleStatus
from model_orchestrator.services.crunch_service import CrunchScheduleService


class TestCrunchScheduleServiceWithPatch:

    def test_detect_schedule_changes(self):
        """
        Tests detecting schedule changes in Crunch objects under various states (e.g., IN_SCHEDULE, NO_SCHEDULE).
        """
        crunch1 = MagicMock()
        crunch2 = MagicMock()

        crunch1.get_schedule_status.return_value = ScheduleStatus.IN_SCHEDULE
        crunch2.get_schedule_status.return_value = ScheduleStatus.NO_SCHEDULE

        crunch_service = MagicMock()
        crunch_service.crunches = {"c1": crunch1, "c2": crunch2}

        model_state_config = MagicMock()

        # Initialize the service.
        schedule_service = CrunchScheduleService(crunch_service, model_state_config)

        # First call: Only crunch1 should report a change since the internal state is empty.
        changes = schedule_service.detect_schedule_changes()
        assert "c1" in changes
        assert changes["c1"] == ScheduleStatus.IN_SCHEDULE
        # crunch2 returns "no_schedule" thus it's intentionally skipped.
        assert "c2" not in changes

        # Second call: with no change, nothing should be detected.
        changes = schedule_service.detect_schedule_changes()
        assert changes == {}

        # Change the status of crunch1.
        crunch1.get_schedule_status.return_value = ScheduleStatus.OUT_OF_SCHEDULE
        changes = schedule_service.detect_schedule_changes()
        assert "c1" in changes
        assert changes["c1"] == ScheduleStatus.OUT_OF_SCHEDULE

    def test_apply_schedule_changes_to_models_out_of_schedule(self):
        """
        Tests applying schedule changes to models when the crunch goes OUT_OF_SCHEDULE.
        """
        crunch1 = MagicMock()
        crunch1.get_schedule_status.return_value = ScheduleStatus.OUT_OF_SCHEDULE
        crunch_service = MagicMock()
        crunch_service.crunches = {"c1": crunch1}

        # Create two dummy model configuration objects as MagicMock instances.
        # Assume each model config has attributes: crunch_id and desired_state.
        model_config1 = MagicMock()
        model_config1.crunch_id = "c1"
        model_config1.desired_state = ModelRun.DesiredStatus.STOPPED  # initial state

        model_config2 = MagicMock()
        model_config2.crunch_id = "c1"
        model_config2.desired_state = ModelRun.DesiredStatus.RUNNING  # initial state

        # Patch the model_state_config to return our dummy model configs.
        model_state_config = MagicMock()
        model_state_config.fetch_configs_as_state.return_value = [model_config1, model_config2]

        schedule_service = CrunchScheduleService(crunch_service, model_state_config)
        updated_configs, _ = schedule_service.apply_schedule_changes_to_models()

        # For OUT_OF_SCHEDULE, the implementation sets all models to STOPPED.
        assert "c1" in updated_configs
        assert len(updated_configs["c1"]) == 2
        for config in updated_configs["c1"]:
            # Check that the desired_state on each updated config is set to ModelRun.DesiredStatus.STOPPED.
            assert config.desired_state == ModelRun.DesiredStatus.STOPPED

    def test_apply_schedule_changes_to_models_in_schedule(self):
        """
        Tests applying schedule changes to models when the crunch is IN_SCHEDULE and expected to RUN.
        """
        crunch1 = MagicMock()
        crunch1.get_schedule_status.return_value = ScheduleStatus.IN_SCHEDULE
        crunch_service = MagicMock()
        crunch_service.crunches = {"c1": crunch1}

        # Create two dummy model configuration objects as MagicMock instances.
        # Assume each model config has attributes: crunch_id and desired_state.
        model_config1 = MagicMock()
        model_config1.crunch_id = "c1"
        model_config1.desired_state = ModelRun.DesiredStatus.STOPPED  # initial state

        model_config2 = MagicMock()
        model_config2.crunch_id = "c1"
        model_config2.desired_state = ModelRun.DesiredStatus.RUNNING  # initial state

        # Patch the model_state_config to return our dummy model configs.
        model_state_config = MagicMock()
        model_state_config.fetch_configs_as_state.return_value = [model_config1, model_config2]

        schedule_service = CrunchScheduleService(crunch_service, model_state_config)
        updated_configs, _ = schedule_service.apply_schedule_changes_to_models()

        # For OUT_OF_SCHEDULE, the implementation sets all models to STOPPED.
        assert "c1" in updated_configs
        assert len(updated_configs["c1"]) == 1
        for config in updated_configs["c1"]:
            # Check that the desired_state on each updated config is set to ModelRun.DesiredStatus.STOPPED.
            assert config.desired_state == ModelRun.DesiredStatus.RUNNING

    def test_apply_schedule_changes_no_change(self):
        """
        Tests that no model configurations are updated when a crunch has NO_SCHEDULE state
        """

        # Create a crunch that returns "no_schedule" (thus no changes).
        crunch1 = MagicMock()
        crunch1.get_schedule_status.return_value = ScheduleStatus.NO_SCHEDULE
        crunch_service = MagicMock()
        crunch_service.crunches = {"c1": crunch1}

        # Create a dummy model config.
        model_config = MagicMock()
        model_config.crunch_id = "c1"
        model_config.desired_state = ModelRun.DesiredStatus.RUNNING

        model_state_config = MagicMock()
        model_state_config.fetch_configs_as_state.return_value = [model_config]

        schedule_service = CrunchScheduleService(crunch_service, model_state_config)
        updated_configs, _ = schedule_service.apply_schedule_changes_to_models()

        # Since there are no schedule changes (- no crunch returns a valid
        # schedule state), no model configurations should be updated.
        assert updated_configs == {}


if __name__ == "__main__":
    pytest.main()
