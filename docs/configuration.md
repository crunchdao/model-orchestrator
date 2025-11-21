# Configuration

A lot of element can be configured to make the model orchestrator work either locally (using Docker) or in the Cloud (using AWS Infrastructure).

# Options

```yaml
logging:
  # Logging levels: 'debug', 'trace', 'info', 'warn', 'error'
  level: debug

  # Logging to a file, remove (or comment) to disable
  file:
    # File path to store application logs
    path: "...."

infrastructure:

  # Configure the database (only one)
  database:
    # Data store type, only `sqlite` available for the moment
    type: sqlite

    # Path of the .db file
    path: "orchestrator.db"

  # Configure publishers
  publishers:
    - type: websocket

      # IP address to bind to
      address: "0.0.0.0"

      # Port to bind to
      port: 9091
      
      # The time interval in seconds at which the server sends ping messages to WebSocket clients to keep the connection alive.
      ping-interval: 5
      
      # The maximum duration in seconds to wait for a client's response to a ping message before considering the connection inactive.
      ping-timeout: 2

    - type: rabbitmq

      # URL to connect to
      url: "amqps://user:password@host/virtual-host/"

  # Configure an aws builder and runner (only one)
  runner:
    type: aws

  # Configure a local builder and runner (only one)
  runner:
    type: local

    # Directory path format where the submission content are stored
    submission-storage-path-format: "storage/submissions/{id}"

    # Directory path format where the resource content are stored
    resource-storage-path-format: "storage/models/{id}"

# Configure the watcher
watcher:

  # Check interval, in seconds
  interval: 10

  # Configure the on-chain model state poller (only one)
  poller:
    type: onchain

    # URL of web-to-3
    url: "http://localhost:3000"

  # Configure the local yaml file model state poller (only one)
  poller:
    type: yaml

    # File path of the file to watch
    path: "models-configs.yml"

# Enable the validation of signature, meant to be used with the onchain poller
signature-verifier:

  # Public key to validate the signed messages
  public-key: "e18a27536919fa9717393522ced3c78476965a498bab7d94e5e07a7dce1c5cc8"

# Control the quarantine system. If a model crashes multiple times, it will be placed into quarantine.
# While crashing is expected during development, set it to false in such cases.
can-place-in-quarantine: true
```

## Development Tips

### Models not stopping

**💣 Be sure to stop all running models to prevent infinite execution loops!**

> [!NOTE]
> By using the `local` runner, all running models will be stopped on exit.

### Rebuild

During the development process, it is often necessary to rebuild images because the code has changed or the [model-runner](https://github.com/crunchdao/model-runner) has been updated.

If you are using the `model-orchestrator dev` command, you can use the `--rebuild <mode>` option:
| Mode | Action |
| --- | --- |
| `disabled` (default) | Never trigger a rebuild. |
| `from-scratch` | Rebuild the image fully and ignore all previous cached layers. |
| `from-checkpoint` | Rebuild the image after the build argument `CRUNCHDAO_BUILD`. <br />If the build argument is not used, act the same way as `if-code-modified`. |
| `if-code-modified` | Rebuild the image and instruct Docker to use all caching layers. <br />This does nothing if the code has not changed. |

> [!NOTE]
> This is only available for the local runner.

### Local model states

By default, the model orchestrator uses on-chain data to manage model operations; however, you can leverage a YAML file to simplify this process by directly providing the desired configuration values.
To enable this, copy the example to [`models-configs.yml`](../model_orchestrator/configuration/example.models.dev.yml), which is the file name expected by the orchestrator. Then, update the `watcher.poller.path` field in `orchestrator.yml` to the YAML file.

### Signature verification

Additionally, if you want to disable signature verification you can comment the `signature-verifier` property:
```yaml
# signature-verifier:
#   public-key: "xxx"
```

# Production Ready

## Configure

1. Configure the AWS credentials:
    ```bash
    $ aws configure
    AWS Access Key ID:  # mandatory
    AWS Secret Access Key:  # mandatory
    Default region name [None]: eu-west-1  # can be something else
    Default output format [None]:  # optional
    ```
2. Update the Orchestrator's configuration:
    ```yaml
    logging:
      level: info

    infrastructure:
      database:
        type: sqlite
        path: "orchestrator.db"

      publishers:
        - type: websocket
          address: "0.0.0.0"
          port: 9091

        # Important to configure!
        - type: rabbitmq
          url: "amqps://user:password@host/virtual-host/"

      runner:
        type: aws

    watcher:
      interval: 10
      poller:
        type: onchain
        # Must target a web-to-3 instance
        url: "http://localhost:3000"

    signature-verifier:
      public-key: "e18a27536919fa9717393522ced3c78476965a498bab7d94e5e07a7dce1c5cc8"
    ```

    > [!NOTE]
    > **Why is the RabbitMQ required?** <br />
    > All events are also shared on RabbitMQ to allow their integration into the tournament's web platform.

## Run on the server

Until CI/CD is created, you need to pull the code and build it into a Docker image on the AWS EC2 instance named `model-orchestrator`.

The instance is organized as follows:
- `$HOME`
   - `/code`: contains the code
   - `/app`: contains the `docker-compose.yml` and .env file
   - `/data`: mounted to the Docker container and contains the database file and configuration file (`orchestrator.yml` and `newrelic.ini`)

1. Build the code:
   ```bash
   cd $HOME/code/models-orchestrator
   ./build.sh
   ```

2. Deploy:
   ```bash
   cd $HOME/app 
   docker-compose up -d --force-recreate
   ```

# AWS

In order to deploy on AWS, the `infrastructure` section of a Crunch must be properly configured.

```yaml
crunches:

  # The slug on the platform, also serves as the ID in the database.
- id: "synth"

  # A human-readable name used for logging.
  name: "synth"

  # The infrastructure block.
  infrastructure:

    # Specify the AWS region to use. If set to "local," the block should only contain the zone key.
    zone: "eu-west-1"

    # Configure the specifications for the CPU instances.
    cpu-config:
      vcpus: 1
      memory: 2048

    # Configure the specifications for the GPU instances.
    gpu-config:
      vcpus: 1
      memory: 2048
      gpus: 1
      instances-types:
      - "g4dn.8xlarge"

  # Configure the networking settings.
  network-config:

    # Coordinator's subnet for the instances to use.
    subnets: ['subnet-05a8cf8a13f8e6a03']

    # Coordinator's security groups for the instances to use.
    security_groups: ['sg-0fe8f6c007cd145e3']

    # Should individual instances be assigned public IPs? (Default to true for backward compatibility).
    assign-public-ip: false
```
