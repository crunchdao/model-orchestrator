import asyncio
import http
import json
from collections import defaultdict

import newrelic.agent
import websockets
from websockets import ServerConnection, Response, Request, Headers
from websockets.exceptions import ConnectionClosed

from model_orchestrator.entities.model_run import ModelRun
from model_orchestrator.mediators.models_state_mediator import \
    ModelsStateMediator
from model_orchestrator.state.models_state_observer import ModelsStateObserver
from model_orchestrator.utils.logging_utils import get_logger

from ...utils.compat import QueueShutDown


class WebSocketServer(ModelsStateObserver):
    QUEUE_WORKER_TIMEOUT = 10  # 10 seconds
    QUEUE_WORKER_MAXSIZE = 1000

    def __init__(self,
                 model_state_mediator: ModelsStateMediator,
                 address="localhost",
                 port=8000,
                 queue_worker_timeout=QUEUE_WORKER_TIMEOUT,
                 queue_worker_maxsize=QUEUE_WORKER_MAXSIZE,
                 ping_interval=None,
                 ping_timeout=None):

        # to prevent direct communication with service

        self.model_state_mediator = model_state_mediator

        # Store WebSocket connections by crunch_id
        self.clients_by_crunch = defaultdict(set)

        # Server address and port
        self.server = None
        self.address = address
        self.port = port

        # queue to handle none async calls
        self.message_queue = asyncio.Queue(queue_worker_maxsize)
        self.queue_worker_timeout = queue_worker_timeout
        self.task_queue_worker = None
        self.loop = None

        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

    async def __aenter__(self):
        await self.server()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    async def _process_request(self, ws: ServerConnection, request: Request) -> Response | None:
        """
        Return an HTTP response for non-WS probes
        """
        path = request.path
        # Must be an upgrade to websocket
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return Response(
                status_code=http.HTTPStatus.UPGRADE_REQUIRED,
                reason_phrase="Upgrade Required",
                headers=Headers([("Content-Type", "text/plain; charset=utf-8")]),
                body=b"Upgrade Required: use WebSocket\n",
            )

        return None  # proceed with WS handshake

    @newrelic.agent.web_transaction(name="WebSocketServer.manual.handler")
    async def handler(self, ws: ServerConnection):
        """
        WebSocket handler to manage clients and send model events.
        """
        # Extract crunch_id or assign to "default"
        path = ws.request.path
        crunch_id = path.strip("/")
        if not crunch_id:
            await ws.send(json.dumps({"error": "crunch_id is required and is expected in the path as /<crunch_id>"}))
            return

        newrelic.agent.add_custom_attribute("crunch_id", crunch_id)

        client_id = id(ws)
        get_logger().debug(f"New client WS connection for crunch_id: {crunch_id}, client_id: {client_id}")
        self.clients_by_crunch[crunch_id].add(ws)
        get_logger().debug(f"Total clients WS connected for crunch_id: {crunch_id} => {len(self.clients_by_crunch[crunch_id])}")

        try:
            # On connection, send `init` event with current model states
            await self.send_init_event(ws)

            # Keep the connection alive by waiting for incoming messages
            async for message in ws:
                message = json.loads(message)
                self.treat_client_message(client_id, crunch_id, message)

        except ConnectionClosed as e:
            newrelic.agent.record_exception(e)
            get_logger().debug(f"WS Connection closed for crunch_id: {crunch_id}, client_id: {client_id}, reason: {e}")
        finally:
            # Cleanup disconnected client
            self.clients_by_crunch[crunch_id].remove(ws)
            if not self.clients_by_crunch[crunch_id]:
                del self.clients_by_crunch[crunch_id]

    async def send_init_event(self, ws):
        """
        Send the `init` event with the current state of all models.
        """

        init_message = {
            'event': 'init',
            'data': [
                self._create_model_state_message(model_run, model_run.runner_status)
                for model_run in self.model_state_mediator.get_running_models()
            ],
        }
        await ws.send(json.dumps(init_message))

    async def send_update_event(self, crunch_id: str, data: list[dict]):
        """
        Broadcast an `update` event to all connected clients of a specific crunch.
        """
        update_message = {
            "event": "update",
            "data": data
        }

        await self.broadcast(crunch_id, json.dumps(update_message))

    async def broadcast(self, crunch_id: str, message: str):
        # Broadcast the update to all connected clients of the specified crunch_id
        clients = self.clients_by_crunch.get(crunch_id, [])
        if len(clients) == 0:
            get_logger().debug(f"No clients connected for crunch_id: {crunch_id}")

        for ws in clients:
            try:
                with newrelic.agent.FunctionTrace(name="WebSocketServer.broadcast"):
                    await ws.send(message)
                    newrelic.agent.add_custom_attribute("crunch_id", crunch_id)
                    get_logger().debug(f"Message published to WS Client:{id(ws)} for crunch:{crunch_id}, message: {message}")
            except ConnectionClosed:
                pass

    async def serve(self):
        """
        Start the WebSocket server and model state simulations.
        """
        self.loop = asyncio.get_running_loop()
        self.task_queue_worker = self.loop.create_task(self._process_queue())

        get_logger().debug(f"Starting WebSocket server on {self.address}:{self.port}, ping_interval:{self.ping_interval}, ping_timeout:{self.ping_timeout}")
        self.server = await websockets.serve(self.handler,
                                             self.address,
                                             self.port,
                                             process_request=self._process_request,
                                             ping_interval=self.ping_interval,
                                             ping_timeout=self.ping_timeout)

    def _create_model_state_message(self, model_run: ModelRun, state: ModelRun.RunnerStatus) -> dict:
        model_name = model_run.name
        cruncher_name = model_run.cruncher_id
        if model_run.augmented_info:
            model_name = model_run.augmented_info.name
            cruncher_name = model_run.augmented_info.cruncher_name

        return {
            'deployment_id': model_run.id,
            'model_id': model_run.model_id,
            'infos': {
                'model_name': model_name,
                'cruncher_id': model_run.cruncher_id,
                'cruncher_name': cruncher_name,
            },
            'state': str(state.value),
            'ip': model_run.ip,
            'port': model_run.port
        }

    def on_runner_state_changed(self, model_run: ModelRun, previous_state: ModelRun.RunnerStatus, new_state: ModelRun.RunnerStatus):
        if new_state == ModelRun.RunnerStatus.RUNNING:
            get_logger().debug(f"notifying client for model_run: {model_run.model_id} is running")
            self.loop.call_soon_threadsafe(self.message_queue.put_nowait, (model_run.crunch_id, [self._create_model_state_message(model_run, ModelRun.RunnerStatus.RUNNING)]))

        elif new_state in [ModelRun.RunnerStatus.STOPPING, ModelRun.RunnerStatus.STOPPED, ModelRun.RunnerStatus.FAILED]:
            get_logger().debug(f"notifying client for model_run: {model_run.model_id} is stopped")
            self.loop.call_soon_threadsafe(self.message_queue.put_nowait, (model_run.crunch_id, [self._create_model_state_message(model_run, ModelRun.RunnerStatus.STOPPED)]))

    def on_build_state_changed(self, model_run: ModelRun, previous_state: ModelRun.BuilderStatus, new_state: ModelRun.BuilderStatus):
        pass  # no require to notify clients

    def notify_failure(self, model_run, failure):
        pass  # no require to notify clients

    def treat_client_message(self, client_id, crunch_id, message):
        get_logger().debug(f"Message received from WS Client:{client_id} for crunch:{crunch_id}, message: {message}")
        if 'event' in message and message['event'] == 'report_failure':
            data = message['data']
            for model in data:
                self.model_state_mediator.report_failure(failure_code=model['failure_code'], model_id=model['model_id'], ip=model['ip'])

    async def _process_queue(self):
        stop = False
        while True:
            try:
                # Potential message loss expected: The clients must reinitialize upon disconnection
                crunch_id, message = await self.message_queue.get()
                await self.send_update_event(crunch_id, message)
            except QueueShutDown:
                get_logger().debug("Queue shut down, exiting task_queue_worker.")
                stop = True
                break
            except Exception as e:
                get_logger().error(f"Error while processing WebSocket message: {e}")
            finally:
                if not stop:
                    self.message_queue.task_done()

    async def shutdown_queue_async(self):
        # shutdown of the queue asynchronously
        await self.loop.run_in_executor(None, self.message_queue.shutdown)

    async def close(self):
        get_logger().debug(f"Waiting for the queue to empty before closing WebSocket Server... {self.message_queue.qsize()}")
        try:
            await asyncio.wait_for(self.shutdown_queue_async(), timeout=self.queue_worker_timeout)
        except asyncio.TimeoutError:
            get_logger().warning("Timeout while waiting for WebSocket task_queue_worker to finish.")
        except Exception as e:
            get_logger().error(f"Unexpected error in WebSocket task_queue_worker during shutdown: {e}")

        if self.server:
            self.server.close()
            await self.server.wait_closed()
            get_logger().debug("WebSocket server has been closed.")


async def main():
    server = WebSocketServer(None)

    # Start the WebSocket server
    await server.serve()

    # Test client to connect and print messages
    async def test_client():
        async with websockets.connect("ws://localhost:8000/test_crunch") as ws:
            print("Test client connected")
            try:
                async for message in ws:
                    print("Received from server:", message)
            except ConnectionClosed:
                print("Test client disconnected")

    asyncio.create_task(test_client())

    print("WebSocket server started on ws://localhost:8000")
    await asyncio.Future()  # Keep the server running


if __name__ == "__main__":
    asyncio.run(main())
