#!/usr/bin/env python3
"""
Fetch models from the local SQLite database for a given crunch name and delete their Docker images from ECR.

Usage:
    docker exec -it <container> python /app/scripts/cleanup_ecr_images.py <crunch_name> [--dry-run]
"""

import argparse
import sys

import boto3
import sqlite_utils


ECR_REPOSITORY = "crunchers-models"
ECR_REGION = "eu-west-1"


def fetch_submission_ids(db_path: str, crunch_name: str) -> list[str]:
    db = sqlite_utils.Database(db_path)
    rows = db.execute(
        """
        SELECT DISTINCT mr.code_submission_id
        FROM model_runs mr
        JOIN crunches c ON c.id = mr.crunch_id
        WHERE c.name = ?
          AND mr.code_submission_id IS NOT NULL
        """,
        [crunch_name],
    ).fetchall()
    return [row[0] for row in rows]


def delete_ecr_images(submission_ids: list[str], repository: str, region: str, dry_run: bool):
    ecr = boto3.client("ecr", region_name=region)

    image_ids = [{"imageTag": sid} for sid in submission_ids]

    # batch_delete_image supports up to 100 at a time
    for i in range(0, len(image_ids), 100):
        batch = image_ids[i : i + 100]

        if dry_run:
            tags = [img["imageTag"] for img in batch]
            print(f"[dry-run] would delete {len(batch)} images: {tags}")
            continue

        resp = ecr.batch_delete_image(
            repositoryName=repository,
            imageIds=batch,
        )

        for success in resp.get("imageIds", []):
            print(f"  deleted: {success.get('imageTag', success.get('imageDigest'))}")

        for failure in resp.get("failures", []):
            print(
                f"  failed:  {failure['imageId'].get('imageTag')} "
                f"— {failure['failureCode']}: {failure['failureReason']}",
                file=sys.stderr,
            )


def main():
    parser = argparse.ArgumentParser(description="Delete ECR images for a crunch's models")
    parser.add_argument("crunch_name", help="Crunch name to query from the database")
    parser.add_argument("--db-path", default="/app/data/orchestrator.db", help="Path to the SQLite database")
    parser.add_argument("--ecr-repository", default=ECR_REPOSITORY)
    parser.add_argument("--ecr-region", default=ECR_REGION)
    parser.add_argument("--dry-run", action="store_true", help="List images without deleting")
    args = parser.parse_args()

    print(f"Fetching submissions for crunch '{args.crunch_name}' from database...")
    submission_ids = fetch_submission_ids(args.db_path, args.crunch_name)

    if not submission_ids:
        print("No submissions found.")
        return

    print(f"Found {len(submission_ids)} submission(s):")
    for sid in submission_ids:
        print(f"  - {sid}")

    print()
    delete_ecr_images(submission_ids, args.ecr_repository, args.ecr_region, args.dry_run)
    print("Done.")


if __name__ == "__main__":
    main()