import logging
import unittest
from unittest.mock import AsyncMock, Mock, patch
import asyncio
import websockets
import json

from model_orchestrator.entities import ModelRun, CruncherOnchainInfo
from model_orchestrator.infrastructure.messaging.websocket_server import WebSocketServer
from model_orchestrator.utils.logging_utils import get_logger

get_logger().setLevel(logging.DEBUG)


class TestWebSocketServer(unittest.TestCase):
    def setUp(self):
        """ Setup synchronous context before each test """
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        self.model_state_mediator_mock = Mock()
        models =  [
            ModelRun(
                id="mock_model_id_20250915_143034_791",
                model_id='mock_model_id',
                name='mock_model_name',
                crunch_id='test_crunch',
                cruncher_onchain_info=CruncherOnchainInfo(wallet_pubkey="test_cruncher_id", hotkey=""),
                code_submission_id='test_code_submission_id',
                resource_id="test_resource_id",
                hardware_type=ModelRun.HardwareType.CPU,
                desired_status=ModelRun.DesiredStatus.RUNNING,
                runner_status=ModelRun.RunnerStatus.RUNNING,

                ip='127.0.0.1',
                port=8080,
            )
        ]
        self.model_state_mediator_mock.get_running_models_by_crunch_id.return_value = models
        self.model_state_mediator_mock.get_running_models.return_value = models

        self.server = WebSocketServer(
            model_state_mediator=self.model_state_mediator_mock,
            address='localhost',
            port=8765
        )

        self.server_task = self.loop.create_task(self.server.serve())
        self.loop.run_until_complete(asyncio.sleep(1))

    def tearDown(self):
        self.loop.run_until_complete(self.server.close())
        self.server_task.cancel()
        try:
            self.loop.run_until_complete(self.server_task)
        except asyncio.CancelledError:
            pass
        self.loop.close()

    async def async_test_events(self):
        async with websockets.connect("ws://localhost:8765/test_crunch") as client:
            init_msg = await client.recv()
            self.assertEqual(json.loads(init_msg),
                             {"event": "init",
                              "data": [{"deployment_id": "mock_model_id_20250915_143034_791", "model_id": "mock_model_id", "infos": {"cruncher_hotkey": "",  "cruncher_wallet_pubkey": "test_cruncher_id", "model_name": "mock_model_name", "cruncher_id": "test_cruncher_id", "cruncher_name": "test_cruncher_id"}, "state": "RUNNING", "ip": "127.0.0.1", "port": 8080}]})

            self.server.on_runner_state_changed(self.model_state_mediator_mock.get_running_models.return_value[0], ModelRun.RunnerStatus.RUNNING, ModelRun.RunnerStatus.STOPPED)

            # Receive the broadcast message
            upd_msg = await client.recv()
            self.assertEqual(json.loads(upd_msg),
                             {"event": "update",
                              "data": [{"deployment_id": "mock_model_id_20250915_143034_791", "model_id": "mock_model_id", "infos": {"cruncher_hotkey": "",  "cruncher_wallet_pubkey": "test_cruncher_id", "model_name": "mock_model_name", "cruncher_id": "test_cruncher_id", "cruncher_name": "test_cruncher_id"}, "state": "STOPPED", "ip": "127.0.0.1", "port": 8080}]})

            await client.send(json.dumps({"event": "report_failure", "data": [{"model_id": "mock_model_id", "failure_code": "CONNECTION_FAILED", "ip": "127.0.0.1"}]}))

        self.model_state_mediator_mock.report_failure.assert_called_once_with(failure_code='CONNECTION_FAILED',
                                                                              model_id='mock_model_id',
                                                                              ip='127.0.0.1')

    def test_events(self):
        self.loop.run_until_complete(self.async_test_events())


if __name__ == '__main__':
    unittest.main()
