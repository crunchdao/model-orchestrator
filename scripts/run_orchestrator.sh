#you need to set the NEW_RELIC_LICENSE_KEY in the .env file
#e.g. NEW_RELIC_LICENSE_KEY=<your-license-key>

export $(cat .env | xargs) && poetry run model-orchestrator
