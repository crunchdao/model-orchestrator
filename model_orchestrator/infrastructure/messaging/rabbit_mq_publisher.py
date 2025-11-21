import asyncio
import json
from typing import Dict

import aio_pika
import newrelic

from model_orchestrator.entities.model_run import ModelRun
from model_orchestrator.state.models_state_observer import ModelsStateObserver
from model_orchestrator.utils.logging_utils import get_logger

from ...utils.compat import QueueShutDown

logger = get_logger()


class RabbitMQPublisher(ModelsStateObserver):
    """
    Implementation of the ModelsStateObserver interface that publishes model state events 
    to RabbitMQ.
    """

    QUEUE_WORKER_TIMEOUT = 10  # 10 seconds
    QUEUE_WORKER_MAXSIZE = 1000
    DEPLOYMENT_QUEUE_NAME = "deployment.model-orchestrator-event.updated"

    def __init__(self, rabbitmq_url: str,
                 deployment_queue_name=DEPLOYMENT_QUEUE_NAME,
                 queue_worker_timeout=QUEUE_WORKER_TIMEOUT,
                 queue_worker_maxsize=QUEUE_WORKER_MAXSIZE):

        self.rabbitmq_url = rabbitmq_url
        self.deployment_queue_name = deployment_queue_name

        # RabbitMQ connection setup
        self.rabbitmq_connection = None
        self.rabbitmq_channel = None

        # queue to handle none async calls
        self.message_queue = asyncio.Queue(queue_worker_maxsize)
        self.queue_worker_timeout = queue_worker_timeout
        self.task_queue_worker = None
        self.loop = None

    async def __aenter__(self):
        await self.run()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    # @classmethod
    # async def create(cls, rabbitmq_url: str, deployment_queue_name=DEPLOYMENT_QUEUE_NAME):
    #     """
    #     Asynchronous factory method to initialize RabbitMQPublisher and automatically execute `setup`.
    #     """
    #     instance = cls(rabbitmq_url, deployment_queue_name)
    #     await instance.setup()
    #     return instance

    async def run(self):
        """
           Initializes the RabbitMQ connection and queue declaration.
        """
        await self._setup_connection()
        self.loop = asyncio.get_running_loop()
        self.task_queue_worker = self.loop.create_task(self._process_queue())

    async def _setup_connection(self):
        """
           Initializes the RabbitMQ connection and queue declaration.
        """
        get_logger().debug("Connecting to RabbitMQ with URL: " + self.rabbitmq_url + " ...")
        self.rabbitmq_connection = await aio_pika.connect_robust(self.rabbitmq_url)
        self.rabbitmq_channel = await self.rabbitmq_connection.channel()
        # await self.rabbitmq_channel.declare_queue(self.deployment_queue_name, durable=True)
        get_logger().debug("Successfully connected to RabbitMQ.")

    @newrelic.agent.background_task()
    async def _process_queue(self):
        """
        Processes events from the async queue and publishes them to RabbitMQ.
        Includes fault-tolerant handling of disconnections and backoff retries.
        """
        exchange = None
        pending_message = None
        logger.info("Starting RabbitMQ queue worker...")
        while True:
            try:
                if not pending_message:
                    message = await self.message_queue.get()
                    pending_message = message
                else:
                    message = pending_message
                    get_logger().debug(f"Message not processed, will retry: {pending_message}")

                if not self.rabbitmq_channel or self.rabbitmq_connection.is_closed:
                    get_logger().warning("Reconnecting to RabbitMQ...")
                    await self._setup_connection()
                    exchange = None

                if exchange is None:
                    exchange = await self.rabbitmq_channel.get_exchange("model-orchestrator")

                await exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(message.get('payload')).encode(),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    routing_key=message.get("queue_name"),
                )
                get_logger().debug(f"Message published to RabbitMQ: {message}")
                pending_message = None
                self.message_queue.task_done()
            except QueueShutDown:
                get_logger().debug("Queue shut down, exiting task_queue_worker.")
                break
            except Exception as e:
                get_logger().error(f"Error while publishing to RabbitMQ: {e}, retrying in 1 second...")
                await asyncio.sleep(1)

    @staticmethod
    def construct_message(model_run: ModelRun, status: str, statusMessage: str = "") -> Dict:
        """
        Constructs a message compatible with `ModelRunUpdateMessage`.
        """
        return {
            "id": model_run.id,
            "desiredStatus": model_run.desired_status.name,
            "status": status,
            "statusMessage": statusMessage,
            "modelId": model_run.model_id,
            "submissionId": model_run.code_submission_id,
            "hardwareType": model_run.hardware_type.value,
            "builderLogUri": model_run.builder_logs_arn,
            "runnerLogUri": model_run.runner_logs_arn,
        }

    def on_runner_state_changed(self, model_run: ModelRun, previous_state: ModelRun.RunnerStatus, new_state: ModelRun.RunnerStatus):
        """
        Logic to execute when a Runner state changes.
        """
        get_logger().debug(f"Runner state changed for ModelRun ID {model_run.id}: {previous_state} -> {new_state}")

        # Create and enqueue a message based on the state change.
        message = self.construct_message(
            model_run=model_run,
            status=f'RUNNER_{new_state.value}',
        )
        self.loop.call_soon_threadsafe(self.message_queue.put_nowait, {'payload': message, 'queue_name': self.deployment_queue_name})

    def on_build_state_changed(self, model_run: ModelRun, previous_state: ModelRun.BuilderStatus, new_state: ModelRun.BuilderStatus):
        """
        Logic to execute when a Builder state changes.
        """
        get_logger().debug(f"Build state changed for ModelRun ID {model_run.id}: {previous_state} -> {new_state}")

        # Create and enqueue a message based on the state change.
        message = self.construct_message(
            model_run=model_run,
            status=f'BUILDER_{new_state.value}',
        )
        self.loop.call_soon_threadsafe(self.message_queue.put_nowait, {'payload': message, 'queue_name': self.deployment_queue_name})

    def notify_failure(self, model_run: ModelRun, failure):
        get_logger().debug(f"Reporting failure via RabbitMq of ModelRun ID %s, with failure %s:%s", model_run.id, failure.error_code, failure.reason)
        message = self.construct_message(
            model_run=model_run,
            status=f'RUNNER_{ModelRun.BuilderStatus.FAILED.value}' if model_run.in_run_phase() else f'BUILDER_{ModelRun.RunnerStatus.FAILED.value}',
            statusMessage=f'{failure.error_code.value}:{failure.reason}' if failure else ''
        )
        self.loop.call_soon_threadsafe(self.message_queue.put_nowait, {'payload': message, 'queue_name': self.deployment_queue_name})

    async def shutdown_queue_async(self):
        # shutdown of the queue asynchronously
        await self.loop.run_in_executor(None, self.message_queue.shutdown)

    async def close(self):
        """
        Cleans up the RabbitMQ connection.
        """
        get_logger().debug(f"Waiting for the queue to empty before closing RabbitMQ connection... {self.message_queue.qsize()}")
        try:
            await asyncio.wait_for(self.shutdown_queue_async(), timeout=self.queue_worker_timeout)
        except asyncio.TimeoutError:
            get_logger().warning("Timeout while waiting for RabbitMQ task_queue_worker to finish.")
        except Exception as e:
            get_logger().error(f"Unexpected error in RabbitMQ task_queue_worker during shutdown: {e}")

        if self.rabbitmq_connection:
            await self.rabbitmq_connection.close()
            get_logger().debug("RabbitMQ connection has been closed.")


async def main():
    import time

    # rabbit_publisher = await RabbitMQPublisher.create(rabbitmq_url="amqps://model-coordinator:kU7870SNqAJW4NVmhelN2ryO3tp2xalF@b-b795b904-d7ed-477a-85ea-be73d070c4a3.mq.eu-west-1.amazonaws.com:5671//tournament-staging/")
    async with RabbitMQPublisher(rabbitmq_url="amqps://model-coordinator:kU7870SNqAJW4NVmhelN2ryO3tp2xalF@b-b795b904-d7ed-477a-85ea-be73d070c4a3.mq.eu-west-1.amazonaws.com:5671//tournament-staging/") as rabbit_publisher:
        frank_model = ModelRun(None, '8805', 'frank', 'bird-game', '1234', '16970', '', ModelRun.HardwareType.CPU, ModelRun.DesiredStatus.RUNNING)
        frank_model.update_builder_status('arn:aws:codebuild:eu-west-1:728958649654:build/model-builder:58a5cdd5-9de4-4fbb-abb3-12dd439a7877', ModelRun.BuilderStatus.BUILDING)
        frank_model.set_builder_logs_arn('arn:aws:ecs:eu-west-1:728958649654:log-group:codebuild-logs:log-stream:model-builder/58a5cdd5-9de4-4fbb-abb3-12dd439a7877')

        # print('Publishing frank_model is building to RabbitMQ...')
        # rabbit_publisher.on_build_state_changed(frank_model, None, frank_model.builder_status)
        # await asyncio.sleep(5)

        # print('Publishing frank_model is success to RabbitMQ...')
        # rabbit_publisher.on_build_state_changed(frank_model, None, ModelRun.BuilderStatus.SUCCESS)
        # await asyncio.sleep(5)

        print('Publishing frank_model is initialized to RabbitMQ...')
        frank_model.update_runner_status('arn:aws:ecs:eu-west-1:728958649654:task/bird-game/8dec680b856142eca87fbe22a8f2abae', ModelRun.RunnerStatus.INITIALIZING)
        frank_model.set_runner_logs_arn('arn:aws:ecs:eu-west-1:728958649654:log-group:/aws/ecs/task:log-stream:bird-game/crunchers-models--2025011401/8dec680b856142eca87fbe22a8f2abae')
        rabbit_publisher.on_runner_state_changed(frank_model, None, frank_model.runner_status)
        # await asyncio.sleep(5)

        print('Publishing frank_model is running to RabbitMQ...')
        rabbit_publisher.on_runner_state_changed(frank_model, None, ModelRun.RunnerStatus.RUNNING)

        await asyncio.sleep(1)