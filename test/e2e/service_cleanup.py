# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Cleans up resources created by the test run.

In addition to tearing down the bootstrapped resources, this sweeps any DSQL
clusters left behind by the e2e tests (for example when a test failed before
its teardown ran, or the controller never reconciled a deletionProtection
change). The sweep disables deletion protection before deleting, so it works
even on clusters that the CR-driven teardown could not remove.

A cluster is only swept if it is ACK-tagged *and* older than
SWEEP_MIN_AGE_SECONDS (1 day by default). The age gate makes the sweep safe to
run against an account shared with other concurrent e2e runs: those runs' live
clusters are only minutes old, well under the threshold, so they are never
touched. Anything a day old can only be a genuine orphan from an earlier run.

This module is invoked by the ACK pytest runner *after* the test session
finishes, regardless of whether the tests passed or failed, so it is the
authoritative safety net against orphaned clusters.
"""

import datetime
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

from acktest.bootstrapping import Resources

from e2e import bootstrap_directory

# Tags that identify a cluster as created by the ACK e2e test suite. The
# cluster*.yaml resources set ManagedBy=ACK and the boto3-created peer cluster
# in the multi-region peered test sets ManagedBy=ACK-e2e. The ACK controller
# additionally stamps system tags prefixed with "services.k8s.aws/".
ACK_MANAGED_BY_VALUES = {"ACK", "ACK-e2e"}
ACK_SYSTEM_TAG_PREFIX = "services.k8s.aws/"

# Cluster states where the cluster is already being torn down — nothing to do.
ALREADY_DELETING_STATES = {"DELETING", "DELETED", "PENDING_DELETE"}

# Only clusters at least this old are considered leftovers. A test run never
# lasts this long, so any ACK-tagged cluster older than this is an orphan and
# clusters created by a concurrently-running test suite (minutes old) are
# always spared. Overridable via DSQL_SWEEP_MIN_AGE_SECONDS.
SWEEP_MIN_AGE_SECONDS = int(
    os.environ.get("DSQL_SWEEP_MIN_AGE_SECONDS", 24 * 60 * 60)
)

# DSQL rejects mutations while a cluster is transitioning, so the sweep retries
# a handful of times, waiting between attempts.
SWEEP_MAX_ATTEMPTS = 6
SWEEP_WAIT_SECONDS = 20


def _sweep_regions():
    """Return the regions that may contain leftover ACK test clusters.

    Tests run in AWS_REGION and the multi-region peered test creates a peer
    cluster in PEER_REGION. Witness regions never host a cluster.
    """
    regions = []
    default_region = (
        os.environ.get("AWS_REGION")
        or boto3.session.Session().region_name
        or "us-west-2"
    )
    regions.append(default_region)

    peer_region = os.environ.get("PEER_REGION", "us-east-1")
    if peer_region not in regions:
        regions.append(peer_region)

    return regions


def _is_ack_test_cluster(cluster: dict) -> bool:
    """Return True if the cluster was created by the ACK e2e test suite."""
    cluster_tags = cluster.get("tags") or {}
    if cluster_tags.get("ManagedBy") in ACK_MANAGED_BY_VALUES:
        return True
    if any(key.startswith(ACK_SYSTEM_TAG_PREFIX) for key in cluster_tags):
        return True
    return False


def _cluster_age_seconds(cluster: dict):
    """Return how many seconds ago the cluster was created, or None if unknown.

    GetCluster returns creationTime as a timezone-aware datetime. We compare
    against the current UTC time to derive the age.
    """
    creation_time = cluster.get("creationTime")
    if creation_time is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if creation_time.tzinfo is None:
        creation_time = creation_time.replace(tzinfo=datetime.timezone.utc)
    return (now - creation_time).total_seconds()


def _is_leftover_cluster(cluster: dict) -> bool:
    """Return True if the cluster is an orphan safe to sweep.

    A cluster qualifies only when it is ACK-tagged AND old enough that it
    cannot belong to an in-progress test run. If creationTime is missing we
    err on the side of caution and do not sweep.
    """
    if not _is_ack_test_cluster(cluster):
        return False
    age = _cluster_age_seconds(cluster)
    if age is None:
        logging.warning(
            "DSQL cluster %s has no creationTime; skipping to be safe",
            cluster.get("identifier"),
        )
        return False
    return age >= SWEEP_MIN_AGE_SECONDS


def _list_cluster_identifiers(client) -> list:
    """Return the identifiers of every cluster in the client's region."""
    identifiers = []
    next_token = None
    while True:
        kwargs = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = client.list_clusters(**kwargs)
        for summary in resp.get("clusters", []):
            identifier = summary.get("identifier")
            if identifier:
                identifiers.append(identifier)
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return identifiers


def _delete_cluster(client, identifier: str):
    """Disable deletion protection and delete a single cluster.

    Retries while the cluster is in a transitional state, since DSQL rejects
    mutations during CREATING/UPDATING/PENDING_SETUP.
    """
    resource_not_found = client.exceptions.ResourceNotFoundException
    for attempt in range(SWEEP_MAX_ATTEMPTS):
        try:
            cluster = client.get_cluster(identifier=identifier)
        except resource_not_found:
            return  # already gone

        status = cluster.get("status")
        if status in ALREADY_DELETING_STATES:
            return

        try:
            if cluster.get("deletionProtectionEnabled"):
                client.update_cluster(
                    identifier=identifier,
                    deletionProtectionEnabled=False,
                )
            client.delete_cluster(identifier=identifier)
            logging.info("Swept leftover DSQL cluster %s", identifier)
            return
        except resource_not_found:
            return
        except ClientError as ex:
            # Most likely the cluster is mid-transition; wait and retry.
            logging.warning(
                "Retrying delete of cluster %s (attempt %d/%d): %s",
                identifier, attempt + 1, SWEEP_MAX_ATTEMPTS, ex,
            )
            time.sleep(SWEEP_WAIT_SECONDS)

    logging.error(
        "Failed to delete leftover DSQL cluster %s after %d attempts",
        identifier, SWEEP_MAX_ATTEMPTS,
    )


def sweep_test_clusters():
    """Delete any ACK-test DSQL clusters left over after the test run."""
    for region in _sweep_regions():
        client = boto3.client("dsql", region_name=region)
        try:
            identifiers = _list_cluster_identifiers(client)
        except ClientError as ex:
            logging.warning("Could not list DSQL clusters in %s: %s", region, ex)
            continue

        for identifier in identifiers:
            try:
                cluster = client.get_cluster(identifier=identifier)
            except client.exceptions.ResourceNotFoundException:
                continue
            except ClientError as ex:
                logging.warning(
                    "Could not describe DSQL cluster %s in %s: %s",
                    identifier, region, ex,
                )
                continue

            if _is_leftover_cluster(cluster):
                _delete_cluster(client, identifier)


def service_cleanup():
    logging.getLogger().setLevel(logging.INFO)

    # Safety net: remove any DSQL clusters the test run left behind, disabling
    # deletion protection first. Runs whether the tests passed or failed.
    try:
        sweep_test_clusters()
    except Exception:
        logging.exception("Error while sweeping leftover DSQL clusters")

    resources = Resources.deserialize(bootstrap_directory)
    resources.cleanup()

if __name__ == "__main__":
    service_cleanup()
