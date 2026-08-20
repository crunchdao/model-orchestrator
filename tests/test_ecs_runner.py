import unittest
from unittest.mock import MagicMock

from model_orchestrator.infrastructure.aws.ecs import AwsEcsRunner


class TestAwsEcsRunnerDescribeStopReason(unittest.TestCase):
    def setUp(self):
        self.runner = AwsEcsRunner(None)
        self.runner.ecs_client = MagicMock()

    def test_stopped_task_returns_reason_string(self):
        self.runner.ecs_client.describe_tasks.return_value = {
            "tasks": [{
                "lastStatus": "STOPPED",
                "stopCode": "ServiceSchedulerInitiated",
                "stoppedReason": "ECS is performing maintenance on the underlying infrastructure hosting the task",
                "containers": [{"exitCode": 137}],
            }]
        }

        result = self.runner.describe_task_stop_reason("numinous", "arn:aws:ecs:eu-west-1:123:task/numinous/abc")

        self.assertEqual(
            result,
            "stopCode=ServiceSchedulerInitiated exitCode=137 reason=ECS is performing maintenance on the underlying infrastructure hosting the task",
        )

    def test_running_task_returns_none(self):
        self.runner.ecs_client.describe_tasks.return_value = {
            "tasks": [{"lastStatus": "RUNNING"}]
        }

        result = self.runner.describe_task_stop_reason("numinous", "arn:aws:ecs:eu-west-1:123:task/numinous/abc")

        self.assertIsNone(result)

    def test_task_not_found_returns_none(self):
        self.runner.ecs_client.describe_tasks.return_value = {"tasks": []}

        result = self.runner.describe_task_stop_reason("numinous", "arn:aws:ecs:eu-west-1:123:task/numinous/abc")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
