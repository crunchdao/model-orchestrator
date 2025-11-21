import asyncio
import collections
import json
import os
from datetime import datetime
from uuid import uuid4

import click

from .configuration import AppConfig, LoggingConfig, RebuildModeStringType
from .entities import ModelRun
from .orchestrator import Orchestrator
from .utils.loader import NotebookUnavailableError, load_notebook
from .utils.logging_utils import get_logger, init_logger

logger = get_logger()


@click.group()
def cli():
    pass


@cli.command()
@click.option("--configuration-file", "configuration_file_path", type=click.Path(exists=True, dir_okay=False), default="orchestrator.yml", help="Path to the main configuration file.", show_default=True)
def start(
    configuration_file_path: str,
):
    configuration = AppConfig.from_file(configuration_file_path)

    init_logger(configuration.logging)

    orchestrator = Orchestrator(configuration)
    asyncio.run(orchestrator.run())


dev_configuration: AppConfig


@cli.group(invoke_without_command=True)
@click.option("--configuration-file", "configuration_file_path", type=click.Path(dir_okay=False), default="orchestrator.dev.yml", help="Path to the main configuration file.", show_default=True)
@click.option("--rebuild", "rebuild_mode", type=click.Choice(RebuildModeStringType.__args__), help="Force rebuild of local docker images, useful for development.")
@click.pass_context
def dev(
    context: click.Context,
    configuration_file_path: str,
    rebuild_mode: RebuildModeStringType,
):
    init_logger(LoggingConfig(level="info", file=None))

    just_created = False
    if not os.path.exists(configuration_file_path):
        logger.info(f"Configuration file `{configuration_file_path}` does not exist. Creating one from template.")

        os.makedirs(os.path.dirname(configuration_file_path) or ".", exist_ok=True)

        from .configuration import example_orchestrator_text
        with open(configuration_file_path, "w") as fd:
            fd.write(example_orchestrator_text)

        just_created = True

    global dev_configuration
    dev_configuration = AppConfig.from_file(configuration_file_path)

    runner_config = dev_configuration.infrastructure.runner

    if rebuild_mode:
        if runner_config.type == "local":
            runner_config.rebuild_mode = rebuild_mode
        else:
            logger.warning(f"Only local runner supports rebuild mode option, ignoring it for {runner_config.type} runner type.")

    if just_created:
        if runner_config.type == "local":
            os.makedirs(os.path.dirname(runner_config.format_submission_storage_path("0") or "."), exist_ok=True)
            os.makedirs(os.path.dirname(runner_config.format_resource_storage_path("0") or "."), exist_ok=True)

        poller_config = dev_configuration.watcher.poller
        if poller_config.type == "yaml":
            os.makedirs(os.path.dirname(poller_config.path) or ".", exist_ok=True)

            from .configuration import example_models_text
            with open(poller_config.path, "w") as fd:
                fd.write(example_models_text)

    init_logger(dev_configuration.logging)

    if context.invoked_subcommand is None:
        logger.info("Starting in development mode...")

        orchestrator = Orchestrator(dev_configuration)
        asyncio.run(orchestrator.run())


@dev.command("import")
@click.argument("notebook_file_path")
@click.option("--import-choice", type=str, help="Choice of how to import the notebook. (new, none, [id])", required=False)
@click.option("--import-name", type=str, help="Submission directory name", required=False)
def import_(
    notebook_file_path: str,
    import_choice: str | None,
    import_name: str | None,
):
    try:
        notebook, notebook_name = load_notebook(notebook_file_path)
    except NotebookUnavailableError as error:
        logger.error(str(error))
        raise click.Abort()

    runner_config = dev_configuration.infrastructure.runner
    if runner_config.type != "local":
        logger.error("Only local runner is supported for import")
        raise click.Abort()

    logger.info("Importing Jupyter Notebook: %s", notebook_file_path)

    from crunch.api import Client
    from crunch.api.auth import NoneAuth
    client = Client(
        api_base_url=dev_configuration.tournament_api_url,
        web_base_url="",  # can be ignored
        auth=NoneAuth(),
    )

    from crunch.convert import extract_cells
    (
        source_code,
        embed_files,
        requirements,
    ) = extract_cells(
        notebook.get("cells", []),
        print=logger.debug
    )

    id = str(uuid4()) if import_name is None else import_name

    base_dir = runner_config.format_submission_storage_path(id)
    os.makedirs(base_dir, exist_ok=True)

    with open(os.path.join(base_dir, "notebook.py"), "w") as fd:
        fd.write(json.dumps(notebook, indent=4))

    with open(os.path.join(base_dir, "main.py"), "w") as fd:
        fd.write(source_code)

    for embed_file in embed_files:
        file_path = os.path.join(base_dir, embed_file.path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w") as fd:
            fd.write(embed_file.content)

    requirements_txt = client.libraries.freeze_imported_requirements(requirements=requirements)
    with open(os.path.join(base_dir, "requirements.txt"), "w") as fd:
        fd.write(requirements_txt)

    logger.info("Imported to: %s", base_dir)

    poller_config = dev_configuration.watcher.poller
    if poller_config.type != "yaml":
        logger.warning("Automatic update of the YAML file is only supported for YAML pollers.")
        return

    choices = collections.OrderedDict()
    choices["Don't include it in the models list"] = False
    choices["Include it as a NEW model in the list"] = True

    from .infrastructure.config_watcher import ModelStateConfigYamlPolling
    poller = ModelStateConfigYamlPolling(
        crunch_names=[crunch.name for crunch in dev_configuration.crunches],
        file_path=poller_config.path,
        on_config_change_callback=None,
        stop_event=None
    )  # TODO: Move those parameters in the function themselves

    model_states = poller.fetch_configs_as_state()

    for model_state in model_states:
        choices[f"Replace the model {model_state.name} (id: {model_state.id}) "] = model_state

    if not import_choice:
        import inquirer
        questions = inquirer.List(
            'update_list',
            message="What do you want to do with the imported model?",
            choices=list(choices.keys()),
        )

        answers = inquirer.prompt([questions], raise_keyboard_interrupt=True)
        model_state = choices[answers["update_list"]]  # type: ignore

    else:
        if import_choice == "none":
            model_state = False
        elif import_choice == "new":
            model_state = True
        else:
            model_state = next((model_state for model_state in model_states if model_state.id == import_choice), None)

    if model_state == False:
        return

    if model_state == True:
        crunch_ids = {model_state.crunch_id for model_state in model_states}
        if not len(crunch_ids):
            crunch_ids = {crunch.id for crunch in dev_configuration.crunches}

        if not len(crunch_ids):
            crunch_ids = {crunch.id for crunch in dev_configuration.crunches}

        if len(crunch_ids) == 0:
            logger.error("No crunches found in the configuration, cannot not automatically add it to the file.")
            raise click.Abort()
        elif len(crunch_ids) == 1:
            crunch_id = next(iter(crunch_ids))
        else:
            questions = inquirer.List(
                'crunch_id',
                message="For which crunch do you want to use for this model?",
                choices=crunch_ids,
            )

            answers = inquirer.prompt([questions], raise_keyboard_interrupt=True)
            crunch_id = answers["crunch_id"]  # type: ignore

        name = datetime.now().isoformat() + "__" + notebook_name

        poller.append(
            id=str(uuid4()),
            name=name,
            submission_id=id,
            crunch_id=crunch_id,
        )
    else:
        poller.update(
            model_state.id,
            submission_id=id,
            desired_state=ModelRun.DesiredStatus.RUNNING,
        )

    logger.info("Model state updated in the YAML file.")


if __name__ == "__main__":
    cli()
