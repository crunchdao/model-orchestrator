from model_orchestrator.configuration.properties import AppConfig
from model_orchestrator.repositories.crunch_repository import CrunchRepository
from model_orchestrator.services import Builder, Runner
from model_orchestrator.utils.logging_utils import get_logger

from ..entities import CpuConfig, Crunch, GpuConfig, Infrastructure, RunnerType, ModelRun
from ..entities.crunch import RunSchedule, ScheduleStatus, CoordinatorInfo
from ..infrastructure.config_watcher import ModelStateConfigPolling, ModelStateConfigOnChainPolling
from model_orchestrator.infrastructure.config_watcher import ModelStateConfigOnChain

logger = get_logger(__name__)


class CrunchNotFoundError(Exception):

    def __init__(self, crunch_id: str):
        super().__init__(f"Crunch with ID '{crunch_id}' not found.")

        self.crunch_id = crunch_id


class CrunchService:

    def __init__(
        self,
        crunch_repository: CrunchRepository,
        model_runner: Runner,
        model_builder: Builder,
        config: AppConfig
    ):
        self.crunch_repository = crunch_repository
        self.model_runner = model_runner
        self.model_builder = model_builder
        self.config = config

        self.create_crunches()

        self.crunches = {
            crunch.id: crunch
            for crunch in crunch_repository.load_active()
        }

    def get_crunch(self, crunch_id: str) -> Crunch:
        crunch = self.crunches.get(crunch_id)

        if crunch is None:
            raise CrunchNotFoundError(crunch_id)

        return crunch

    def get_crunch_from_onchain_name(self, onchain_name: str) -> Crunch:
        for crunch in self.crunches.values():
            if crunch.onchain_name == onchain_name:
                return crunch

        raise CrunchNotFoundError(onchain_name)

    # todo creation of ECS environment (cluster)
    def create_crunches(self):

        # Get runner type from infrastructure config
        runner_type_str = self.config.infrastructure.runner.type
        runner_type_map = {"aws": RunnerType.AWS_ECS, "local": RunnerType.LOCAL, "phala": RunnerType.PHALA}
        runner_type = runner_type_map.get(runner_type_str)
        if runner_type is None:
            raise ValueError(f"Unknown runner type: {runner_type_str}")

        config_crunches = []

        for crunch_config in self.config.crunches:
            if not crunch_config.infrastructure:
                raise ValueError(f"Missing infrastructure configuration for crunch {crunch_config.id}")

            # For local and phala runners, don't create CPU/GPU configs
            if runner_type in (RunnerType.LOCAL, RunnerType.PHALA):
                infrastructure = Infrastructure(
                    cluster_name=crunch_config.infrastructure.cluster_name,
                    zone=crunch_config.infrastructure.zone,
                    runner_type=runner_type,
                    cpu_config=None,
                    gpu_config=None
                )
            else:
                # For AWS runner, require full infrastructure config
                if not crunch_config.infrastructure.cpu_config or not crunch_config.infrastructure.gpu_config:
                    raise ValueError(f"AWS runner requires full infrastructure configuration for crunch {crunch_config.id}")

                infrastructure = Infrastructure(
                    cluster_name=crunch_config.infrastructure.cluster_name,
                    zone=crunch_config.infrastructure.zone,
                    runner_type=runner_type,
                    cpu_config=CpuConfig(
                        vcpus=crunch_config.infrastructure.cpu_config.vcpus,
                        memory=crunch_config.infrastructure.cpu_config.memory,
                        instances_types=crunch_config.infrastructure.cpu_config.instances_types
                    ),
                    gpu_config=GpuConfig(
                        vcpus=crunch_config.infrastructure.gpu_config.vcpus,
                        memory=crunch_config.infrastructure.gpu_config.memory,
                        instances_types=crunch_config.infrastructure.gpu_config.instances_types,
                        gpus=crunch_config.infrastructure.gpu_config.gpus
                    ),
                    is_secure=crunch_config.infrastructure.is_secure,
                    debug_grpc=crunch_config.infrastructure.debug_grpc
                )

            if self.config.watcher.poller.type == "onchain" and not crunch_config.onchain_name:
                logger.warning(f"Crunch {crunch_config.id} has no onchain name, using crunch id instead")
                crunch_config.onchain_name = crunch_config.id

            crunch = Crunch(
                id=crunch_config.id,
                name=crunch_config.name,
                onchain_name=crunch_config.onchain_name,
                infrastructure=infrastructure,
                network_config=crunch_config.network_config,
                run_schedule=RunSchedule.from_config(crunch_config.run_schedule) if crunch_config.run_schedule else None,
            )

            config_crunches.append(crunch)

        for crunch in config_crunches:
            get_logger().debug(f'creation/update of crunch:{crunch}')
            crunch.runner_config = self.model_runner.create(crunch)
            crunch.builder_config = self.model_builder.create(crunch)

            self.crunch_repository.save(crunch)

    def update_onchain_infos(self, crunch, config: ModelStateConfigOnChain):
        updated = False

        if crunch.onchain_address != config.crunch_address:
            crunch.onchain_address = config.crunch_address
            updated = True

        new_info = CoordinatorInfo(
            wallet_pubkey=config.coordinator_wallet_pubkey,
            cert_hash=config.coordinator_cert_hash,
            cert_hash_secondary=config.coordinator_cert_hash_secondary,
        )
        if crunch.coordinator_info != new_info:
            crunch.coordinator_info = new_info
            updated = True

        if updated:
            self.crunch_repository.save(crunch)


class CrunchScheduleService:
    """
    Service responsible for detecting and applying schedule-related state changes to Crunch objects and their corresponding model configurations.
    """

    def __init__(
        self,
        crunch_service: CrunchService,
        model_state_config: ModelStateConfigPolling
    ):
        self.crunch_service = crunch_service
        self.model_state_config = model_state_config
        self._last_known_schedule_states = {}

    def detect_schedule_changes(self):
        """
        Checks each Crunch to see if its schedule state differs from the last known state. Returns a dict mapping from crunch_id to the new state for those Crunch objects that changed.
        """

        changed_schedule_states = {}
        for crunch_id, crunch in self.crunch_service.crunches.items():
            current_state = crunch.get_schedule_status()
            # Skip if no schedule applies to this Crunch.
            if current_state == ScheduleStatus.NO_SCHEDULE:
                continue

            # Record only if the state changed since we last saw it.
            if self._last_known_schedule_states.get(crunch_id) != current_state:
                self._last_known_schedule_states[crunch_id] = current_state
                changed_schedule_states[crunch_id] = current_state

        return changed_schedule_states

    def apply_schedule_changes_to_models(self):
        """
        Fetches Crunch schedule state changes and applies them to model
        configurations, adjusting desired states where appropriate.

        :return: A list of model configurations that were updated.
        """

        changed_schedule_states = self.detect_schedule_changes()
        if not changed_schedule_states:
            return {}, {}

        updated_model_configs = {}
        model_configs = self.model_state_config.fetch_configs_as_state()
        grouped_model_configs = {
            crunch_id: [
                model_config for model_config in model_configs
                if model_config.crunch_id == (self.crunch_service.crunches[crunch_id].onchain_name
                                              if isinstance(self.model_state_config, ModelStateConfigOnChainPolling)
                                              else crunch_id)
            ]
            for crunch_id in changed_schedule_states.keys()
        }
        for crunch_id, state in changed_schedule_states.items():
            configs_for_crunch = grouped_model_configs.get(crunch_id)
            if not configs_for_crunch:
                continue

            # Regardless of the model's current desired state (running or stopped), we ensure all models are stopped.
            # This approach accounts for situations where a model might have desired to stop, but we didn't switch to "out of schedule" in time.
            if state == ScheduleStatus.OUT_OF_SCHEDULE:
                for config in configs_for_crunch:
                    config.desired_state = ModelRun.DesiredStatus.STOPPED
                    updated_model_configs.setdefault(crunch_id, []).append(config)

            # In this case, only activate the models that are expected to be running
            elif state == ScheduleStatus.IN_SCHEDULE:
                for config in configs_for_crunch:
                    if config.desired_state == ModelRun.DesiredStatus.RUNNING:
                        updated_model_configs.setdefault(crunch_id, []).append(config)

        return updated_model_configs, changed_schedule_states
