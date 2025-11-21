import boto3
import json

from model_orchestrator.utils.logging_utils import get_logger


class SecretManager:
    def __init__(self, region_name):
        self.client = boto3.client("secretsmanager", region_name=region_name)

    def get_secret(self, secret_name):
        try:
            response = self.client.get_secret_value(SecretId=secret_name)
            secret = response["SecretString"]
            return json.loads(secret)
        except Exception as e:
            get_logger().error(f"Error retrieving secret {secret_name}", exc_info=True)

        return None

    def get_docker_credentials(self):
        secrets = self.get_secret('docker-hub-token')
        docker_username, docker_token = None, None

        if secrets:
            docker_username = secrets.get("username")
            docker_token = secrets.get("token")

        return docker_username, docker_token


if __name__ == "__main__":
    region = "eu-west-1"
    print(SecretManager(region).get_docker_credentials())
