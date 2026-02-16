import asyncio
from collections import defaultdict
import signal
import threading
from pathlib import Path

from model_orchestrator.entities import ModelRun
from model_orchestrator.infrastructure.config_watcher import ModelStateConfig
from model_orchestrator.infrastructure.db.sqlite import (
    SQLiteCrunchRepository, SQLiteModelRunRepository)
from model_orchestrator.infrastructure.tournament import TournamentApi
from model_orchestrator.mediators.models_state_mediator import \
    ModelsStateMediator
from model_orchestrator.services.crunch_service import CrunchNotFoundError, CrunchService, CrunchScheduleService
from model_orchestrator.services.model_runs import (ModelRunsService,
                                                    SignatureVerifier)
from model_orchestrator.state.models_state_subject import ModelsStateSubject
from model_orchestrator.utils.logging_utils import get_logger, attach_uvicorn_to_my_logger

from ..configuration import AppConfig
from ..infrastructure import ThreadPoller
from ..infrastructure.config_watcher._onchain import ModelStateConfigOnChain
from ..repositories.augmented_model_info_repository import DisabledAugmentedModelInfoRepository
from ..utils.compat import add_signal_handler

import newrelic.agent

file_path = Path(__file__).parent

logger = get_logger()


class Orchestrator:

    def __init__(self, configuration: AppConfig):
        self.config = configuration

        self.stop_event = asyncio.Event()
        self.threads_stop_event = threading.Event()

        self._starters = []
        self._finishers = []

        self._backgrounds = []
        self._daemons = []

        database_config = self.config.infrastructure.database
        if database_config.type == "sqlite":
            model_repository = SQLiteModelRunRepository(database_config.path)
            crunch_repository = SQLiteCrunchRepository(database_config.path)
        else:
            raise ValueError(f"Unsupported database: {database_config}")

        augmented_model_repository = TournamentApi(self.config.tournament_api_url) if self.config.use_augmented_info else DisabledAugmentedModelInfoRepository()

        if True:
            logger.debug("Configuring builder and runner...")

            runner_config = configuration.infrastructure.runner
            if runner_config.type == "aws":
                logger.info("Configuring AWS builder and runner...")

                from ..infrastructure.aws import AwsCodeBuildModelBuilder
                model_builder = AwsCodeBuildModelBuilder(runner_config)

                from ..infrastructure.aws import AwsEcsModelRunner
                model_runner = AwsEcsModelRunner()
            elif runner_config.type == "local":
                logger.info("Configuring Local builder and runner...")

                from ..infrastructure.local import LocalModelBuilder, RebuildMode
                model_builder = LocalModelBuilder(
                    submission_storage_path_provider=runner_config.format_submission_storage_path,
                    resource_storage_path_provider=runner_config.format_resource_storage_path,
                    rebuild_mode=RebuildMode.map(runner_config.rebuild_mode),
                )

                from ..infrastructure.local import LocalModelRunner
                model_runner = LocalModelRunner(
                    docker_network_name=runner_config.docker_network_name
                )
            elif runner_config.type == "phala":
                logger.info("Configuring Phala TEE builder and runner...")

                from ..infrastructure.phala import PhalaModelBuilder, PhalaModelRunner, PhalaMetrics
                from ..infrastructure.phala._cluster import PhalaCluster

                db_dir = str(Path(configuration.infrastructure.database.path).parent)
                phala_metrics = PhalaMetrics(db_path=f"{db_dir}/phala_metrics.db")
                self.phala_metrics = phala_metrics

                # Initialize cluster: discovers CVMs from Phala API (or fallback URLs)
                cluster = PhalaCluster(
                    cluster_name=runner_config.cluster_name,
                    spawntee_port=runner_config.spawntee_port,
                    request_timeout=runner_config.request_timeout,
                    phala_api_url=runner_config.phala_api_url,
                    registry_compose_path=runner_config.registry_compose_path,
                    runner_compose_path=runner_config.runner_compose_path,
                    fallback_urls=runner_config.cluster_urls,
                    instance_type=runner_config.instance_type,
                    memory_per_model_mb=runner_config.memory_per_model_mb,
                    provision_factor=runner_config.provision_factor,
                    max_models=runner_config.max_models,
                )
                cluster.discover()
                cluster.rebuild_task_map()
                self.phala_cluster = cluster

                model_builder = PhalaModelBuilder(cluster, metrics=phala_metrics)
                model_runner = PhalaModelRunner(cluster, metrics=phala_metrics)
            else:
                raise ValueError(f"Unknown runner type: {runner_config.type}")

        self.crunch_service = CrunchService(
            crunch_repository=crunch_repository,
            model_runner=model_runner,
            model_builder=model_builder,
            config=configuration
        )

        crunch_names = set(self.crunch_service.crunches.keys())
        logger.debug(f"Configuring watcher for {len(crunch_names)} crunch(s)...")

        poller_config = configuration.watcher.poller
        if poller_config.type == "yaml":
            from ..infrastructure.config_watcher import \
                ModelStateConfigYamlPolling
            logger.debug("Configuring watcher: YAML")

            self.model_config_poller = ModelStateConfigYamlPolling(
                file_path=poller_config.path,
                crunch_names=crunch_names,
                on_config_change_callback=self.on_model_state_changed,
                interval=configuration.watcher.interval,
                stop_event=self.threads_stop_event
            )

            if configuration.infrastructure.runner.type == "local":
                augmented_model_repository = self.model_config_poller

            self._backgrounds.append(self.model_config_poller.start_polling)
        elif poller_config.type == "onchain":
            from ..infrastructure.config_watcher import \
                ModelStateConfigOnChainPolling
            logger.info("Configuring watcher: OnChain")

            crunch_onchain_names = set([crunch.onchain_name for crunch in self.crunch_service.crunches.values()])
            self.model_config_poller = ModelStateConfigOnChainPolling(
                url=poller_config.url,
                crunch_names=crunch_onchain_names,
                on_config_change_callback=self.on_model_state_changed,
                interval=configuration.watcher.interval,
                stop_event=self.threads_stop_event
            )

            self._backgrounds.append(self.model_config_poller.start_polling)

        else:
            raise ValueError(f"Unsupported watcher type: {poller_config.type}")


        self.models_run_service = ModelRunsService(
            model_runs_repository=model_repository,
            crunch_repository=crunch_repository,
            builder=model_builder,
            runner=model_runner,
            state_subject=ModelsStateSubject(),
            augmented_model_info_repository=augmented_model_repository,
            app_config=configuration
        )

        self.model_state_mediator = ModelsStateMediator(self.models_run_service)

        if runner_config.type == "local":
            self._finishers.append(self.stop_all_models)

        signature_verifier_config = configuration.signature_verifier
        self.model_signature_verifier = (
            SignatureVerifier(public_key=signature_verifier_config.public_key)
            if signature_verifier_config
            else None
        )

        if not self.model_signature_verifier and configuration.watcher.poller == 'onchain':
            get_logger().warning("Model signature verification IS DISABLED! The data coming from the blockchain will not be verified.")

        self._backgrounds.append(self.update_model_states)

        if True:
            logger.debug("Configuring publishers...")

            for publisher_config in configuration.infrastructure.publishers:
                if publisher_config.type == "websocket":
                    logger.debug("Configuring publisher: WebSocket")

                    from ..infrastructure.messaging.websocket_server import \
                        WebSocketServer

                    publisher = WebSocketServer(
                        model_state_mediator=self.model_state_mediator,
                        address=publisher_config.address,
                        port=publisher_config.port,
                        ping_interval=publisher_config.ping_interval,
                        ping_timeout=publisher_config.ping_timeout,
                    )

                    self._starters.append(publisher.serve)
                    self._finishers.append(publisher.close)

                    self.models_run_service.state_subject.add_observer(publisher)

                elif publisher_config.type == "rabbitmq":
                    logger.debug("Configuring publisher: RabbitMQ")

                    from ..infrastructure.messaging.rabbit_mq_publisher import \
                        RabbitMQPublisher

                    publisher = RabbitMQPublisher(publisher_config.url)

                    self._starters.append(publisher.run)
                    self._finishers.append(publisher.close)

                    self.models_run_service.state_subject.add_observer(publisher)

            logger.debug("Configured %s publishers", len(configuration.infrastructure.publishers))

        if True:
            self.crunch_schedule_service = CrunchScheduleService(self.crunch_service, self.model_config_poller)
            schedule_poller = ThreadPoller(
                task=lambda: self.on_schedule_changed(*self.crunch_schedule_service.apply_schedule_changes_to_models()),
                interval=60,  # 1min
                stop_event=self.threads_stop_event,
            )
            self._backgrounds.append(schedule_poller.start_polling)

        if True:
            if configuration.infrastructure.runner.type == "local":
                from ..infrastructure.http import create_local_deploy_api, LocalDeployServices
                import uvicorn

                port = 8001
                host = "0.0.0.0"
                logger.info(f"Start HTTP server for local deployment over {host}:{port}")

                # for the local convenience, we start FastAPI to let interaction over HTTP
                local_deploy_services = LocalDeployServices(
                    model_state_mediator=self.model_state_mediator,
                    app_config=configuration,
                    model_state_config=self.model_config_poller
                )
                local_deploy_api = create_local_deploy_api(local_deploy_services)

                def make_uvicorn_runner(app, host, port):
                    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=True)
                    server = uvicorn.Server(config)

                    def uvicorn_run():
                        server.run()

                    def uvicorn_stop():
                        if not server.should_exit:
                            server.should_exit = True
                            #server.force_exit = True  # optional, fast

                    return uvicorn_run, uvicorn_stop

                uvicorn_run, uvicorn_stop = make_uvicorn_runner(local_deploy_api, host, port)
                attach_uvicorn_to_my_logger()
                self._backgrounds.append(uvicorn_run)
                self._finishers.append(uvicorn_stop)

    def on_schedule_changed(self, config_changes: dict, changed_schedule_states):
        if changed_schedule_states:
            get_logger().info(f"Schedule change detected {changed_schedule_states}")

            for crunch_id, config_changes in config_changes.items():
                crunch = self.crunch_service.get_crunch(crunch_id)
                self.handle_model_state_changed(crunch, config_changes)

    def on_model_state_changed(self, config_changes: list[ModelStateConfig]):

        grouped_configs = defaultdict(list)
        for config in config_changes:
            grouped_configs[config.crunch_id].append(config)

        for crunch_id, configs in grouped_configs.items():
            try:
                config = configs[0]  # todo improve by grouping directly ModelStateConfig in a CrunchStateConfig or something like
                if isinstance(config, ModelStateConfigOnChain):
                    crunch = self.crunch_service.get_crunch_from_onchain_name(crunch_id)
                    self.crunch_service.update_onchain_infos(crunch, config)
                else:
                    crunch = self.crunch_service.get_crunch(crunch_id)

            except CrunchNotFoundError:
                get_logger().warning(f"Crunch with id {crunch_id} not found. Skipping model state change handling.")
                continue

            if not crunch.can_model_run_now():
                get_logger().debug(f"Crunch {crunch.name} is currently outside its scheduled running interval. "
                                   f"The model change {[config.id for config in configs]} will be skipped and triggered later when the crunch enters the running interval.")
                continue

            self.handle_model_state_changed(crunch, configs)

    def handle_model_state_changed(self, crunch, config_changes: list[ModelStateConfig]):
        for config in config_changes:
            get_logger().trace(f"Model state change detected: {config}")

            model_id = config.id
            try:
                if self.model_signature_verifier:
                    signature_valid = self.model_signature_verifier.verify_signature(
                        cruncher_id=config.cruncher_id,
                        model_id=model_id,
                        code_submission_id=config.submission_id,
                        resource_id=config.resource_id,
                        signature=config.signature
                    )

                    if not signature_valid:
                        get_logger().warning(f'Model signature verification failed for model id: {model_id}')
                        continue
                    else:
                        get_logger().debug(f'Model signature verification passed for model id: {model_id}')

                desired_state = config.desired_state
                if desired_state == ModelRun.DesiredStatus.RUNNING:
                    self.models_run_service.start_model(
                        model_id=model_id,
                        name=config.name,
                        cruncher_id=config.cruncher_id,
                        code_submission_id=config.submission_id,
                        resource_id=config.resource_id,
                        hardware_type=config.hardware_type,
                        crunch=crunch,
                        cruncher_wallet_pubkey=config.cruncher_wallet_pubkey if isinstance(config, ModelStateConfigOnChain) else None,
                        cruncher_hotkey=config.cruncher_hotkey if isinstance(config, ModelStateConfigOnChain) else None,
                    )

                elif desired_state == ModelRun.DesiredStatus.STOPPED:
                    self.models_run_service.stop_model(
                        model_id=model_id
                    )

            except Exception as e:
                get_logger().exception(f"Error handling model change %s", model_id, exc_info=e)
                continue

    def update_model_states(self):
        interval = self.config.watcher.interval
        while not self.threads_stop_event.is_set():
            self.process_model_state_updates()
            self.threads_stop_event.wait(timeout=interval)
        get_logger().debug("Update model states stopped")

    @newrelic.agent.background_task()
    def process_model_state_updates(self):
        try:
            self.models_run_service.updates_states()
        except Exception as e:
            get_logger().error(f"Error updating model states: {e}", exc_info=e)

    async def run(self):
        loop = asyncio.get_running_loop()
        add_signal_handler(loop, signal.SIGINT, self.shutdown)
        add_signal_handler(loop, signal.SIGTERM, self.shutdown)

        await self._start()

        await self.stop_event.wait()

        await self._stop()

    def shutdown(self):
        logger.info("Shutdown requested")

        self.stop_event.set()
        self.threads_stop_event.set()

    async def stop_all_models(self):
        logger.info("Stopping all running models...")

        for model in self.models_run_service.get_running_models():
            logger.info("Stopping model: %s", model.id)
            self.models_run_service.stop_model(model.model_id)

    async def _start(self):
        logger.debug("-- Starting orchestrator...")

        for starter in self._starters:
            logger.debug("-- Starting %s...", starter.__name__)
            await starter()
        logger.debug("-- Processed starters")

        for background in self._backgrounds:
            thread = threading.Thread(target=background, daemon=True)
            logger.debug("-- Creating %s...", thread)
            self._daemons.append(thread)
        logger.debug("-- Processed backgrounds")

        for thread in self._daemons:
            logger.debug("-- Starting thread %s...", thread)
            thread.start()
        logger.debug("-- Processed daemons")

    async def _stop(self):
        logger.debug("-- Stopping orchestrator...")

        for finisher in self._finishers:
            logger.debug("-- Finishing %s...", finisher.__name__)
            if asyncio.iscoroutinefunction(finisher):
                await finisher()
            else:
                finisher()
        logger.debug("-- Processed finishers")


        for thread in self._daemons:
            logger.debug("Waiting for thread %s...", thread)
            thread.join()
        logger.debug("-- Processed threads")

        # Print Phala metrics summary on shutdown
        if hasattr(self, 'phala_metrics'):
            logger.info(self.phala_metrics.summary())


