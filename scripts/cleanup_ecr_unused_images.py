#!/usr/bin/env python3
"""
Delete ECR images not referenced by any active model run.
Images are tagged by code_submission_id. Any image whose tag is not used
by an active model run (not STOPPED/FAILED) is considered unused.

Usage:
    docker exec -it <container> python /app/scripts/cleanup_ecr_unused_images.py [--dry-run]
"""

import argparse
import sys

import boto3
import sqlite_utils


ECR_REPOSITORY = "crunchers-models"
ECR_REGION = "eu-west-1"


def get_all_active_submission_ids(db_path: str) -> set[str]:
    db = sqlite_utils.Database(db_path)
    rows = db.execute(
        """
        SELECT DISTINCT code_submission_id
        FROM model_runs
        WHERE runner_status NOT IN ('STOPPED', 'FAILED')
          AND code_submission_id IS NOT NULL
        """,
    ).fetchall()
    return {row[0] for row in rows}


def list_ecr_images(ecr, repository: str) -> list[dict]:
    images = []
    paginator = ecr.get_paginator("describe_images")
    for page in paginator.paginate(repositoryName=repository):
        images.extend(page.get("imageDetails", []))
    return images


def cleanup_images(ecr, repository: str, images: list[dict], active_ids: set[str], dry_run: bool) -> int:
    to_delete = []

    for img in images:
        tags = img.get("imageTags", [])
        digest = img.get("imageDigest", "?")
        size_mb = img.get("imageSizeInBytes", 0) / (1024 * 1024)

        if not tags:
            continue

        if any(tag in active_ids for tag in tags):
            print(f"  [keep]    {', '.join(tags)} ({size_mb:.0f} MB)")
            continue

        if dry_run:
            print(f"  [dry-run] would delete {', '.join(tags)} ({size_mb:.0f} MB)")
            to_delete.append(img)
            continue

        to_delete.append({"imageDigest": digest})

    if dry_run:
        return len(to_delete)

    deleted = 0
    for i in range(0, len(to_delete), 100):
        batch = to_delete[i : i + 100]
        resp = ecr.batch_delete_image(repositoryName=repository, imageIds=batch)

        for success in resp.get("imageIds", []):
            print(f"  [deleted] {success.get('imageTag', success.get('imageDigest'))}")
            deleted += 1

        for failure in resp.get("failures", []):
            print(
                f"  [failed]  {failure['imageId'].get('imageTag', failure['imageId'].get('imageDigest'))} "
                f"- {failure['failureCode']}: {failure['failureReason']}",
                file=sys.stderr,
            )

    return deleted


def main():
    parser = argparse.ArgumentParser(description="Delete unused ECR images across all crunches")
    parser.add_argument("--db-path", default="/app/data/orchestrator.db", help="Path to the SQLite database")
    parser.add_argument("--ecr-repository", default=ECR_REPOSITORY)
    parser.add_argument("--ecr-region", default=ECR_REGION)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without doing it")
    args = parser.parse_args()

    active_ids = get_all_active_submission_ids(args.db_path)
    print(f"Active submission IDs across all crunches: {len(active_ids)}")

    ecr = boto3.client("ecr", region_name=args.ecr_region)

    print(f"Listing images in ECR repository '{args.ecr_repository}'...")
    images = list_ecr_images(ecr, args.ecr_repository)

    if not images:
        print("No images found.")
        return

    tagged = [i for i in images if i.get("imageTags")]
    unused = [i for i in tagged if not any(t in active_ids for t in i.get("imageTags", []))]
    print(f"Found {len(tagged)} tagged image(s), {len(unused)} unused\n")

    count = cleanup_images(ecr, args.ecr_repository, images, active_ids, args.dry_run)

    if not args.dry_run:
        print(f"\nDeleted {count} image(s).")


if __name__ == "__main__":
    main()