from .properties import *

import importlib.resources

example_orchestrator_text = importlib.resources.read_text(__package__, 'example.orchestrator.dev.yml')
example_models_text = importlib.resources.read_text(__package__, 'example.models.dev.yml')
