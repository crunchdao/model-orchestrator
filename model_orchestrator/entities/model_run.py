from datetime import datetime
from enum import Enum

from .model_info import ModelInfo
from .failure import Failure


class ModelRun:
    """
    Represents a container or model to be managed by the orchestrator.
    """

    class DesiredStatus(Enum):
        RUNNING = "RUNNING"
        REPLACING = "REPLACING"  # private
        STOPPED = "STOPPED"

    class HardwareType(Enum):
        CPU = "CPU"
        GPU = "GPU"

    class RunnerStatus(Enum):
        INITIALIZING = "INITIALIZING"
        RUNNING = "RUNNING"
        STOPPING = "STOPPING"
        STOPPED = "STOPPED"
        FAILED = "FAILED"

    class BuilderStatus(Enum):
        BUILDING = "BUILDING"
        FAILED = "FAILED"
        SUCCESS = "SUCCESS"

    # todo rename to ModelRun + id unique id + rename id per model_id
    def __init__(
            self,
            id: str | None,
            model_id: str,
            name: str,
            crunch_id: str,
            cruncher_id: str,
            code_submission_id: str,
            resource_id: str,
            hardware_type: HardwareType,
            desired_status: DesiredStatus,
            docker_image: str = None,
            runner_status: RunnerStatus = None,
            builder_status: BuilderStatus = None,
            runner_job_id: str = None,
            builder_job_id: str = None,
            ip: str = None,
            port: int = None,
            created_at: datetime = None,
            last_update: datetime = None,
            runner_info: dict = None,
            runner_logs_arn: str = None,
            builder_logs_arn: str = None,
            failure: Failure = None,
            in_quarantine: bool = False,
            augmented_info: ModelInfo | None = None
        ):
        """
        Expected fields:
        :param model_id: unique id of the model, if the cruncher change the code by new submission of the code, this id will remain unchanged.
        :param name: Name of the model chose per the cruncher
        :param crunch_id: unique id of the crunch (game)
        :param cruncher_id: unique id of the cruncher (public key from blockchain)
        :param code_submission_id: unique if of the submitted code, if the cruncher change the code by new submission of the code, this id will change
        :param resource_id: The cruncher can train their model, serialize it into a specific format, and store it in the resources section. This ID allows access to it.
        :param hardware_type: Type of hardware required for the execution of the model, e.g. CPU, GPU,
        :param desired_status: The desired state of the model, e.g. RUNNING, STOPPED.

        Internal fields:
        :param docker_image: The Docker image built and used for the execution.
        :param runner_status: Internal status of the runner process to flow lifecycle of the model execution
        :param builder_status: Internal status of the builder process to flow lifecycle of the model building.
        :param runner_job_id: Identifier for the runner job.
        :param builder_job_id: Identifier for the builder job.
        :param created_at: Timestamp of the entity creation.
        :param last_update: Timestamp of the last update on the entity.
        :param runner_info: Additional information related to the runner like the cluster name, task execution role ARN, etc.
        :param runner_logs_arn: The ARN of the logs where the runner logs are stored.
        :param builder_logs_arn: The ARN of the logs where the builder logs are stored.
        """
        self.id = f'{model_id}_{datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]}' if id is None else id
        self.model_id = model_id
        self.name = name
        self.crunch_id = crunch_id
        self.cruncher_id = cruncher_id
        self.docker_image = docker_image
        self.code_submission_id = code_submission_id
        self.resource_id = resource_id
        self.hardware_type = hardware_type
        self.desired_status = desired_status
        self.runner_status = runner_status
        self.builder_status = builder_status
        self.builder_job_id = builder_job_id
        self.runner_job_id = runner_job_id
        self.port = port
        self.ip = ip
        self.created_at = created_at if created_at else datetime.now()
        self.last_update = last_update if last_update else datetime.now()

        self.runner_info = runner_info
        self.runner_logs_arn = runner_logs_arn
        self.builder_logs_arn = builder_logs_arn

        self.failure = failure
        self.in_quarantine = in_quarantine

        self.augmented_info = augmented_info

        if not self.code_submission_id or self.code_submission_id.strip() == "":
            raise ValueError(f"Invalid code_submission_id value: {self.code_submission_id}")

    def is_fully_stopped(self) -> bool:
        """
        Returns True if both desired_status and current_status are STOPPED.
        """
        return self.desired_status == ModelRun.DesiredStatus.STOPPED and self.runner_status in [ModelRun.RunnerStatus.STOPPED, ModelRun.RunnerStatus.FAILED]

    def update_desired_status(self, desired: DesiredStatus):
        self.desired_status = desired
        self.last_update = datetime.now()

    def update_runner_status(self, job_id: str | None, state: RunnerStatus, **kwargs):
        """
        Updates the runner status and its corresponding metadata based on the provided
        job ID, current state, and additional keyword arguments. This method modifies
        the internal state of the object, including job ID, status, and network
        configurations such as IP and port, while also recording the last update
        timestamp. In case the state is RUNNING, both 'ip' and 'port' are mandatory
        in the keyword arguments.

        :raises ValueError: If the state is set to `RUNNING` and either 'ip' or 'port'
            is missing in the provided keyword arguments.
        """
        self.runner_job_id = job_id
        self.runner_status = state
        if 'ip' in kwargs:
            self.ip = kwargs['ip']
        if 'port' in kwargs:
            self.port = kwargs['port']

        if state == ModelRun.RunnerStatus.RUNNING and ('ip' not in kwargs or 'port' not in kwargs):
            raise ValueError("Missing ip or port for running model")

        self.last_update = datetime.now()

    def update_builder_status(self, job_id: str | None, current: BuilderStatus):
        self.builder_job_id = job_id
        self.builder_status = current
        self.last_update = datetime.now()

    def has_changed(self, code_submission_id, resource_id, hardware_type):
        #  if you change here, careful to change also model_runs_repository.is_model_in_quarantine
        return (self.code_submission_id != code_submission_id or
                self.resource_id != resource_id or
                self.hardware_type != hardware_type)

    def is_built(self):
        return self.docker_image is not None and self.docker_image != ""

    def set_docker_image(self, docker_image):
        self.docker_image = docker_image
        self.last_update = datetime.now()

    def docker_tag(self):
        return self.code_submission_id  # todo : handle also resource_id ?

    def is_build_required(self, replaced_model) -> bool:
        return self.docker_tag() != replaced_model.docker_tag()

    def set_runner_info(self, runner_info):
        self.runner_info = runner_info
        self.last_update = datetime.now()

    def set_runner_logs_arn(self, runner_logs_arn):
        self.runner_logs_arn = runner_logs_arn
        self.last_update = datetime.now()

    def set_builder_logs_arn(self, builder_logs_arn):
        self.builder_logs_arn = builder_logs_arn
        self.last_update = datetime.now()

    def in_run_phase(self):
        return self.runner_status is not None

    def is_run_active(self):
        return self.runner_status in [ModelRun.RunnerStatus.INITIALIZING, ModelRun.RunnerStatus.RUNNING] and self.runner_job_id

    def is_running(self):
        return self.runner_status in [ModelRun.RunnerStatus.RUNNING] and self.runner_job_id

    def in_build_phase(self):
        return not self.in_run_phase() and self.builder_status is not None

    def record_failure(self, error_code, reason, exception, traceback):
        self.failure = Failure(self.id, error_code, reason, exception, traceback)

    def has_failed(self):
        return self.failure is not None

    def place_in_quarantine(self):
        self.in_quarantine = True

    def is_quarantined(self):
        return self.in_quarantine
