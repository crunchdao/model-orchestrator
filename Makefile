DOCKER_IMAGE=728958649654.dkr.ecr.eu-west-1.amazonaws.com/model-orchestrator

docker-build:
	docker build -t ${DOCKER_IMAGE} .

docker-push:
	docker push ${DOCKER_IMAGE}
