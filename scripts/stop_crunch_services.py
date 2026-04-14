#!/usr/bin/env python3
"""
Stop all running ECS services for a given crunch by reading active models from the database.
The orchestrator will automatically restart them with the latest config.

Usage:
    python scripts/stop_crunch_services.py <crunch_name> [--db-path data/orchestrator.db] [--dry-run]
"""

import argparse
import sys

import boto3
import sqlite_utils
from botocore.exceptions import ClientError


def load_active_models(db_path: str, crunch_name: str) -> list[dict]:
    db = sqlite_utils.Database(db_path)
    rows = db.execute(
        """
        SELECT mr.id, mr.model_id, mr.runner_job_id, mr.runner_info, mr.runner_status
        FROM model_runs mr
        JOIN crunches c ON c.id = mr.crunch_id
        WHERE c.name = ?
          AND mr.runner_status NOT IN ('STOPPED', 'FAILED')
        """,
        [crunch_name],
    ).fetchall()

    columns = ["id", "model_id", "runner_job_id", "runner_info", "runner_status"]
    return [dict(zip(columns, row)) for row in rows]


def parse_runner_info(raw: str) -> dict:
    if not raw:
        return {}
    return eval(raw)


def stop_services(models: list[dict], dry_run: bool):
    ecs_clients: dict[str, boto3.client] = {}
    stopped = 0

    for model in models:
        runner_info = parse_runner_info(model["runner_info"])
        cluster_name = runner_info.get("cluster_name")
        service_name = runner_info.get("service_name", model["runner_job_id"])

        if not cluster_name or not service_name:
            print(f"  [skip] {model['model_id']} - missing cluster_name or service_name")
            continue

        if dry_run:
            print(f"  [dry-run] would stop {service_name} (cluster={cluster_name}, status={model['runner_status']})")
            continue

        region = runner_info.get("region")
        cache_key = region or "default"
        if cache_key not in ecs_clients:
            ecs_clients[cache_key] = boto3.client("ecs", region_name=region)
        ecs = ecs_clients[cache_key]

        try:
            ecs.update_service(
                cluster=cluster_name,
                service=service_name,
                desiredCount=0,
            )
            print(f"  [stopped] {service_name} (cluster={cluster_name})")
            stopped += 1
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ("ServiceNotFoundException", "ServiceNotActiveException"):
                print(f"  [skip] {service_name} - {error_code}")
            else:
                print(f"  [error] {service_name} - {e}", file=sys.stderr)

    return stopped


def main():
    parser = argparse.ArgumentParser(description="Stop all running ECS services for a crunch")
    parser.add_argument("crunch_name", help="Name of the crunch")
    parser.add_argument("--db-path", default="data/orchestrator.db", help="Path to the SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="List services without stopping")
    args = parser.parse_args()

    models = load_active_models(args.db_path, args.crunch_name)

    if not models:
        print(f"No active models found for crunch '{args.crunch_name}'.")
        return

    print(f"Found {len(models)} active model(s) for crunch '{args.crunch_name}':\n")
    for m in models:
        runner_info = parse_runner_info(m["runner_info"])
        service_name = runner_info.get("service_name", m["runner_job_id"])
        print(f"  - {m['model_id']} ({service_name}) [{m['runner_status']}]")

    print()
    stopped = stop_services(models, args.dry_run)

    if not args.dry_run:
        print(f"\nStopped {stopped}/{len(models)} service(s). The orchestrator will restart them with the latest config.")


if __name__ == "__main__":
    main()