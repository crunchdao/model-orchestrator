# TODO

## Completed

- [x] Refactor the hardcoded crunch out of crunch_service and read it from config (commit: cfadfbe)
  - Move hardcoded crunch configurations from `CrunchService.create_hardcoded_crunches()` to `orchestrator.yml`
  - Support multiple crunches in the configuration (mandatory config - cannot run without crunches)
  - Rename `create_hardcoded_crunches()` to `create_crunches()` that reads from config
  - Add `RunnerType.LOCAL` to the enum
  - Read the `RunnerType` from `orchestrator.yml` at `infrastructure.runner.type` and assign it to each crunch
  - Implementation steps:
    1. Add `RunnerType.LOCAL` to enum in `model_orchestrator/entities/crunch.py:5-7`
    2. Create `_crunches.py` config classes in `model_orchestrator/configuration/properties/`
    3. Update `AppConfig` in `model_orchestrator/configuration/properties/__init__.py` - add mandatory `crunches: CrunchesConfig = Field(...)`
    4. Update `orchestrator.yml` to include crunches section with current hardcoded data
    5. Modify `CrunchService` in `model_orchestrator/services/crunch_service.py:38-71` - add config param, rename method, read from config, set runner_type
    6. Update `Orchestrator` in `model_orchestrator/orchestrator/_orchestrator.py:76-80` - pass config to CrunchService

- [x] Validate the models before the image is build (commit: 1a9b49e)
  - WHAT: Validate model submissions before Docker image build, with validation rules per crunch submission_type
    - Train and infer: Validate main.py has train() function (existence only) and infer(payload_stream: typing.Iterator[pandas.DataFrame]) function
    - Infer only: Validate main.py has infer(payload_stream: typing.Iterator[pandas.DataFrame]) function
    - Dynamic_subclass: Skip validation for now
    - On validation failure: Don't build image, log specific error, set model state to FAILED
    - Apply to both AWS and local builders
  - HOW:
    1. Add submission_type field to CrunchConfig in model_orchestrator/configuration/properties/_crunches.py
    2. Add submission_type field to Crunch entity in model_orchestrator/entities/crunch.py
    3. Add SUBMISSION_VALIDATION_FAILED error type in model_orchestrator/entities/errors.py
    4. Create validation service/module to check submission files
    5. Add validation call in _BuildService.build() method in model_orchestrator/services/model_runs/build.py:35-39 before calling self.builder.build()
    6. Handle validation failures through existing error handling in _ErrorHandlingService
    7. Access submission files via submission_storage_path_provider for local, and validate in orchestrator (not buildspec) for AWS

- [x] Remove submission_type and remove the Validate model submissions (commit: pending)
  - WHAT: Remove submission_type and validation system since only dynamic model runners will be used
    - Remove submission_type field from crunch configuration and entities  
    - Remove the SubmissionValidator service entirely and all validation logic
    - Remove validation calls from the build process
    - Remove SUBMISSION_VALIDATION_FAILED error type  
    - Remove submission_type from database schema
    - Clean up validation-related imports and references
    - Update tests to remove validation mocks
  - HOW:
    1. Delete model_orchestrator/services/submission_validator.py file entirely
    2. Remove validation imports and logic from model_orchestrator/services/model_runs/build.py:9,40-50
    3. Remove get_submission_storage_path_provider() method from model_orchestrator/infrastructure/local/_builder.py:38-40 and model_orchestrator/infrastructure/aws/codebuild.py:41-43
    4. Remove SUBMISSION_VALIDATION_FAILED error type from model_orchestrator/entities/errors.py:20,42
    5. Remove submission_type field from model_orchestrator/entities/crunch.py:54
    6. Remove submission_type field from model_orchestrator/configuration/properties/_crunches.py:32
    7. Remove submission_type usage from model_orchestrator/services/crunch_service.py:78
    8. Remove submission_type from database schema in model_orchestrator/infrastructure/db/sqlite/_crunch_repository.py:44,68,135
    9. Remove validation mock from tests/test_model_runs_service.py:12-13

