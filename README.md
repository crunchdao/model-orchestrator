# Model Orchestrator

- [Model Orchestrator](#model-orchestrator)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Running](#running)
- [Going Deeper](#going-deeper)
- [TODOs](#todos)

As its name suggests, the Model Orchestrator facilitates the orchestration of models produced by our crunchers and informs the coordinator of their availability.

Connected to the blockchain, it monitors changes in the desired states of models (Running, Stopped) and is responsible for building and running them.

Continuously connected with the coordinator via WebSocket, it provides real-time updates on all state changes (start, stop) as well as access information such as IP and Port, enabling the coordinator to directly interact with the models.

# Getting Started

## Prerequisites

Make sure you have the following installed:

- **Python 3.13**, or later
- **Poetry**, a package manager ([install](https://python-poetry.org/docs/))
- **AWS CLI**, to configure the credentials ([install](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html))

## Installation

1. Clone the repository:
   ```bash
   $ git clone git@github.com:crunchdao/models-orchestrator.git
   $ cd models-orchestrator
   ```

2. Use Poetry to install the dependencies:
   ```bash
   $ poetry install
   ```

3. Activate the Poetry virtual environment:
   ```bash
   $ poetry shell
   ```

## Running

Launch the `model-orchestrator` using Poetry:

```bash
$ poetry run model-orchestrator dev
```

**In another terminal**, import a notebook. This will ensure everything is working:

```bash
$ poetry run model-orchestrator dev import https://github.com/microprediction/birdgame/blob/main/birdgame/examples/quickstarters/auto_ets_sktime.ipynb
...
What do you want to do with the imported model?:
> Replace the model frank (id: 1)
```

After importing it, go back to the first terminal and you should see it being built then deployed locally.

# Going Deeper

To learn more about configuration and production readyness, consult [configuration.md](./docs/configuration.md).

# TODOs

- [ ] Handle Task not found in AWS: `error occurred (InvalidParameterException) when calling the StopTask operation: The referenced task was not found`
- [ ] Fix hardcoded path to data and split data and configs
- [ ] Define behavior when sending message to WS client raise an Exception
- [ ] When a local run is "STOPPING", it is reporting as an error
