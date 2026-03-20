#!/bin/sh
# Entrypoint script that optionally enables NewRelic monitoring
# If NEW_RELIC_LICENSE_KEY is set, wraps the command with newrelic-admin
# Otherwise, runs the command directly

if [ -n "$NEW_RELIC_LICENSE_KEY" ]; then
    echo "NewRelic monitoring enabled"
    exec newrelic-admin run-program "$@"
else
    exec "$@"
fi