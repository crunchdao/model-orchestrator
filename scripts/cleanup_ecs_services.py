#!/usr/bin/env python3
"""
Delete ECS services with 0 running tasks for a given crunch.

Usage:
    docker exec -it <container> python /app/scripts/cleanup_ecs_services.py <crunch_name> [--dry-run]
"""

import argparse
import sys

import boto3
import sqlite_utils
from botocore.exceptions import ClientError


def get_cluster_name(db_path: str, crunch_name: str) -> str | None:
    db = sqlite_utils.Database(db_path)
    row = db.execute(
        "SELECT cluster_name FROM crunches WHERE name = ?", [crunch_name]
    ).fetchone()
    return row[0] if row else None


def list_cluster_services(ecs, cluster_name: str, crunch_name: str) -> list[dict]:
    service_arns = []
    paginator = ecs.get_paginator("list_services")
    for page in paginator.paginate(cluster=cluster_name):
        service_arns.extend(page.get("serviceArns", []))

    if not service_arns:
        return []

    services = []
    for i in range(0, len(service_arns), 10):
        batch = service_arns[i : i + 10]
        response = ecs.describe_services(cluster=cluster_name, services=batch)
        for svc in response.get("services", []):
            if svc["serviceName"].startswith(f"{crunch_name}--"):
                services.append(svc)

    return services


def cleanup_services(ecs, cluster_name: str, services: list[dict], dry_run: bool) -> int:
    deleted = 0

    for svc in services:
        name = svc["serviceName"]
        running = svc.get("runningCount", 0)

        desired = svc.get("desiredCount", 0)

        if running > 0 or desired > 0:
            print(f"  [keep]    {name} (running={running}, desired={desired})")
            continue

        if dry_run:
            print(f"  [dry-run] would delete {name}")
            continue

        try:
            ecs.delete_service(cluster=cluster_name, service=name, force=True)
            print(f"  [deleted] {name}")
            deleted += 1
        except ClientError as e:
            print(f"  [error]   {name} - {e}", file=sys.stderr)

    return deleted


def main():
    parser = argparse.ArgumentParser(description="Delete ECS services with 0 running tasks for a crunch")
    parser.add_argument("crunch_name", help="Name of the crunch")
    parser.add_argument("--db-path", default="/app/data/orchestrator.db", help="Path to the SQLite database")
    parser.add_argument("--region", default=None, help="AWS region (uses default if not set)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without doing it")
    args = parser.parse_args()

    cluster_name = get_cluster_name(args.db_path, args.crunch_name)
    if not cluster_name:
        print(f"Crunch '{args.crunch_name}' not found in database (or missing cluster_name).")
        return

    ecs = boto3.client("ecs", region_name=args.region)

    print(f"Listing services in cluster '{cluster_name}' for crunch '{args.crunch_name}'...")
    services = list_cluster_services(ecs, cluster_name, args.crunch_name)

    if not services:
        print("No services found.")
        return

    idle = [s for s in services if s.get("runningCount", 0) == 0 and s.get("desiredCount", 0) == 0]
    print(f"Found {len(services)} service(s), {len(idle)} idle (running=0, desired=0)\n")

    deleted = cleanup_services(ecs, cluster_name, services, args.dry_run)

    if not args.dry_run:
        print(f"\nDeleted {deleted} service(s).")


if __name__ == "__main__":
    main()