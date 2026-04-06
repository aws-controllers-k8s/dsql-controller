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

"""Integration tests for the DSQL Cluster resource.

Tests cover the full Cluster lifecycle:
- Create with optional fields (tags)
- Wait for ACTIVE status
- Verify status fields populated (endpoint, encryptionDetails, identifier)
- Add policy, verify PutResourcePolicy called
- Remove policy, verify DeleteResourcePolicy called
- Delete Cluster, verify cleanup
"""

import boto3
import json
import os
import pytest
import time
import logging

from acktest.resources import random_suffix_name
from acktest.k8s import resource as k8s
from acktest.k8s import condition
from acktest import tags
from acktest.aws.identity import get_account_id
from kubernetes.client.rest import ApiException
from e2e import service_marker, CRD_GROUP, CRD_VERSION, load_dsql_resource
from e2e.replacement_values import REPLACEMENT_VALUES

RESOURCE_PLURAL = "clusters"

# DSQL clusters are async — creation can take several minutes
CREATE_WAIT_AFTER_SECONDS = 30
UPDATE_WAIT_AFTER_SECONDS = 10
DELETE_WAIT_AFTER_SECONDS = 10

# Max wait for cluster to reach ACTIVE (up to 10 minutes)
ACTIVE_WAIT_PERIODS = 30
ACTIVE_WAIT_PERIOD_LENGTH = 20  # seconds per period

SAMPLE_POLICY_TEMPLATE = '{{"Version":"2012-10-17","Statement":[{{"Effect":"Allow","Principal":{{"AWS":"arn:aws:iam::{account_id}:root"}},"Action":"dsql:DbConnectAdmin","Resource":"*"}}]}}'


def _make_sample_policy(account_id):
    """Create a valid DSQL resource policy for the given account."""
    return SAMPLE_POLICY_TEMPLATE.format(account_id=account_id)


def _wait_for_cluster_active(ref, wait_periods=ACTIVE_WAIT_PERIODS):
    """Wait for the Cluster to reach ACTIVE status via the Synced condition."""
    return k8s.wait_on_condition(
        ref,
        condition.CONDITION_TYPE_RESOURCE_SYNCED,
        "True",
        wait_periods=wait_periods,
        period_length=ACTIVE_WAIT_PERIOD_LENGTH,
    )


def _get_cluster_status_field(ref, field):
    """Get a field from the Cluster CR status."""
    cr = k8s.get_resource(ref)
    if cr is None:
        return None
    return cr.get("status", {}).get(field)


def _get_cluster_identifier(ref):
    """Get the cluster identifier from the CR status."""
    return _get_cluster_status_field(ref, "identifier")


def _get_aws_cluster(dsql_client, identifier):
    """Get the cluster from AWS using the DSQL API."""
    try:
        return dsql_client.get_cluster(identifier=identifier)
    except dsql_client.exceptions.ResourceNotFoundException:
        return None


def _get_aws_cluster_policy(dsql_client, identifier):
    """Get the cluster policy using the DSQL GetClusterPolicy API."""
    try:
        resp = dsql_client.get_cluster_policy(identifier=identifier)
        return resp.get("policy")
    except dsql_client.exceptions.ResourceNotFoundException:
        return None


def _wait_for_aws_cluster_status(dsql_client, identifier, target_statuses,
                                  max_attempts=ACTIVE_WAIT_PERIODS,
                                  wait_seconds=ACTIVE_WAIT_PERIOD_LENGTH):
    """Poll AWS until the cluster reaches one of the target statuses."""
    for _ in range(max_attempts):
        cluster = _get_aws_cluster(dsql_client, identifier)
        if cluster and cluster.get("status") in target_statuses:
            return cluster
        time.sleep(wait_seconds)
    return None


@pytest.fixture(scope="module")
def simple_cluster(dsql_client):
    """Create a simple Cluster for basic lifecycle tests."""
    resource_name = random_suffix_name("ack-dsql", 24)

    replacements = REPLACEMENT_VALUES.copy()
    replacements["CLUSTER_NAME"] = resource_name

    resource_data = load_dsql_resource(
        "cluster",
        additional_replacements=replacements,
    )
    logging.debug(resource_data)

    ref = k8s.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
        resource_name, namespace="default",
    )
    k8s.create_custom_resource(ref, resource_data)
    cr = k8s.wait_resource_consumed_by_controller(ref)

    assert cr is not None
    assert k8s.get_resource_exists(ref)

    yield (ref, cr)

    # Teardown: disable deletion protection, then delete
    try:
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted
    except Exception:
        pass


@pytest.fixture(scope="module")
def cluster_with_tags(dsql_client):
    """Create a Cluster with additional tags for tag management tests."""
    resource_name = random_suffix_name("ack-dsql-tags", 24)

    replacements = REPLACEMENT_VALUES.copy()
    replacements["CLUSTER_NAME"] = resource_name

    resource_data = load_dsql_resource(
        "cluster_with_tags",
        additional_replacements=replacements,
    )
    logging.debug(resource_data)

    ref = k8s.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
        resource_name, namespace="default",
    )
    k8s.create_custom_resource(ref, resource_data)
    cr = k8s.wait_resource_consumed_by_controller(ref)

    assert cr is not None
    assert k8s.get_resource_exists(ref)

    yield (ref, cr)

    # Teardown: disable deletion protection, then delete
    try:
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted
    except Exception:
        pass


@pytest.fixture(scope="module")
def multi_region_cluster(dsql_client):
    """Create a Cluster with multiRegionProperties (witnessRegion only).

    Without a linked peer cluster, the cluster enters PENDING_SETUP and
    stays there. This fixture yields the ref/cr for verification, then
    cleans up on teardown.
    """
    resource_name = random_suffix_name("ack-dsql-mr", 24)

    replacements = REPLACEMENT_VALUES.copy()
    replacements["CLUSTER_NAME"] = resource_name
    replacements["WITNESS_REGION"] = os.environ.get("WITNESS_REGION", "us-east-2")

    resource_data = load_dsql_resource(
        "cluster_multi_region",
        additional_replacements=replacements,
    )
    logging.debug(resource_data)

    ref = k8s.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
        resource_name, namespace="default",
    )
    k8s.create_custom_resource(ref, resource_data)
    cr = k8s.wait_resource_consumed_by_controller(ref)

    assert cr is not None
    assert k8s.get_resource_exists(ref)

    yield (ref, cr)

    # Teardown: disable deletion protection, then delete
    try:
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted
    except Exception:
        pass


@service_marker
@pytest.mark.canary
class TestCluster:
    """E2E tests for the DSQL Cluster resource lifecycle."""

    def test_create_and_wait_for_active(self, dsql_client, simple_cluster):
        """Test that creating a Cluster CR invokes CreateCluster and reaches ACTIVE."""
        (ref, cr) = simple_cluster

        # Wait for the cluster to become ACTIVE
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach ACTIVE status (ACK.ResourceSynced=True)"
        condition.assert_synced(ref)

        # Verify status fields are populated
        cr = k8s.get_resource(ref)
        assert cr is not None

        status = cr.get("status", {})
        assert status.get("identifier") is not None, "identifier not populated"
        assert status.get("endpoint") is not None, "endpoint not populated"
        assert status.get("status") == "ACTIVE", f"Expected ACTIVE, got {status.get('status')}"
        assert status.get("creationTime") is not None, "creationTime not populated"

        # Verify encryption details are populated
        encryption = status.get("encryptionDetails")
        assert encryption is not None, "encryptionDetails not populated"
        assert encryption.get("encryptionStatus") is not None

        # Verify the cluster exists in AWS
        identifier = status["identifier"]
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None, "Cluster not found in AWS"
        assert aws_cluster.get("status") == "ACTIVE"

    def test_verify_tags_on_create(self, dsql_client, cluster_with_tags):
        """Test that tags specified at creation are applied to the cluster."""
        (ref, cr) = cluster_with_tags

        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach ACTIVE status"
        condition.assert_synced(ref)

        cr = k8s.get_resource(ref)
        identifier = cr["status"]["identifier"]

        # Verify tags in AWS
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None

        # Get tags from the AWS response
        aws_tags = aws_cluster.get("tags") or {}

        expected_tags = {
            "Environment": "testing",
            "Team": "platform",
            "ManagedBy": "ACK",
        }

        for key, value in expected_tags.items():
            assert key in aws_tags, f"Tag '{key}' not found in AWS tags"
            assert aws_tags[key] == value, \
                f"Tag '{key}' expected '{value}', got '{aws_tags[key]}'"

        # Validate ACK system tags are present on the AWS resource
        tags.assert_ack_system_tags(tags=aws_tags)

    def test_add_policy(self, dsql_client, simple_cluster):
        """Test that adding a policy to the spec invokes PutResourcePolicy."""
        (ref, cr) = simple_cluster

        # Ensure cluster is ACTIVE first
        assert _wait_for_cluster_active(ref)
        condition.assert_synced(ref)

        cr = k8s.get_resource(ref)
        identifier = cr["status"].get("identifier")
        assert identifier is not None, "Cluster identifier not available"

        # Get account ID using acktest identity helper
        account_id = get_account_id()
        sample_policy = _make_sample_policy(account_id)

        # Add policy to spec
        updates = {"spec": {"policy": sample_policy}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        # Wait for the controller to reconcile and reach Synced=True
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach Synced=True after policy update"
        condition.assert_synced(ref)

        # Verify the policy in AWS with a single call
        aws_policy = _get_aws_cluster_policy(dsql_client, identifier)
        assert aws_policy is not None, "Policy not found on cluster after sync"

        # Verify the policy content matches (compare as parsed JSON)
        if isinstance(aws_policy, str):
            aws_policy_parsed = json.loads(aws_policy)
        else:
            aws_policy_parsed = aws_policy
        expected_policy_parsed = json.loads(sample_policy)
        assert aws_policy_parsed == expected_policy_parsed, \
            "Policy content does not match"

        # Verify the policy is reflected back in the CR spec
        cr = k8s.get_resource(ref)
        cr_policy = cr.get("spec", {}).get("policy")
        assert cr_policy is not None, "Policy not synced back to CR spec"

    def test_remove_policy(self, dsql_client, simple_cluster):
        """Test that removing the policy from spec invokes DeleteResourcePolicy."""
        (ref, cr) = simple_cluster

        assert _wait_for_cluster_active(ref)
        condition.assert_synced(ref)

        cr = k8s.get_resource(ref)
        identifier = cr["status"].get("identifier")
        assert identifier is not None

        # Ensure a policy is attached first (may already be from test_add_policy)
        current_policy = _get_aws_cluster_policy(dsql_client, identifier)
        if current_policy is None:
            account_id = get_account_id()
            sample_policy = _make_sample_policy(account_id)
            updates = {"spec": {"policy": sample_policy}}
            k8s.patch_custom_resource(ref, updates)
            time.sleep(UPDATE_WAIT_AFTER_SECONDS)
            # Wait for the controller to reconcile the policy addition
            assert _wait_for_cluster_active(ref), \
                "Cluster did not reach Synced=True after adding policy"
            condition.assert_synced(ref)

        # Remove policy by setting to empty string
        updates = {"spec": {"policy": ""}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        # Wait for the controller to reconcile the policy removal
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach Synced=True after policy removal"
        condition.assert_synced(ref)

        # Verify the policy is removed in AWS with a single call
        aws_policy = _get_aws_cluster_policy(dsql_client, identifier)
        assert aws_policy is None, "Policy still attached after removal"

    def test_update_tags(self, dsql_client, cluster_with_tags):
        """Test that modifying tags invokes TagResource/UntagResource."""
        (ref, cr) = cluster_with_tags

        assert _wait_for_cluster_active(ref)
        condition.assert_synced(ref)

        cr = k8s.get_resource(ref)
        identifier = cr["status"]["identifier"]

        # Update tags: add a new tag, modify existing, remove one
        new_tags = {
            "Environment": "staging",  # modified
            "Team": "platform",        # unchanged
            "NewTag": "new-value",     # added
            # "ManagedBy" removed
        }
        updates = {"spec": {"tags": new_tags}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        # Wait for the controller to reconcile the tag update
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach Synced=True after tag update"
        condition.assert_synced(ref)

        # Verify the tags in AWS with a single call
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        aws_tags = aws_cluster.get("tags") or {}

        assert aws_tags.get("Environment") == "staging", \
            f"Expected 'staging', got '{aws_tags.get('Environment')}'"
        assert aws_tags.get("NewTag") == "new-value", \
            "New tag not added"

    def test_delete_cluster(self, dsql_client):
        """Test that deleting the CR invokes DeleteCluster and cleans up."""
        resource_name = random_suffix_name("ack-dsql-del", 24)

        replacements = REPLACEMENT_VALUES.copy()
        replacements["CLUSTER_NAME"] = resource_name

        resource_data = load_dsql_resource(
            "cluster",
            additional_replacements=replacements,
        )

        ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
            resource_name, namespace="default",
        )
        k8s.create_custom_resource(ref, resource_data)
        cr = k8s.wait_resource_consumed_by_controller(ref)
        assert cr is not None

        # Wait for ACTIVE
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach ACTIVE before deletion test"
        condition.assert_synced(ref)

        cr = k8s.get_resource(ref)
        identifier = cr["status"]["identifier"]

        # Verify cluster exists in AWS
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None

        # Disable deletion protection before deleting
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)
        assert _wait_for_cluster_active(ref), \
            "Cluster did not return to Synced after disabling deletion protection"

        # Delete the K8s resource
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted

        # Wait for AWS deletion to complete
        max_attempts = 30
        wait_seconds = 20

        for _ in range(max_attempts):
            time.sleep(wait_seconds)
            aws_cluster = _get_aws_cluster(dsql_client, identifier)
            if aws_cluster is None:
                return
            cluster_status = aws_cluster.get("status")
            if cluster_status == "DELETED":
                return

        pytest.fail(
            f"Cluster {identifier} was not deleted from AWS after "
            f"{max_attempts * wait_seconds} seconds"
        )

    def test_status_fields_populated(self, dsql_client, simple_cluster):
        """Test that all read-only status fields are populated after sync."""
        (ref, cr) = simple_cluster

        assert _wait_for_cluster_active(ref)
        condition.assert_synced(ref)

        cr = k8s.get_resource(ref)
        status = cr.get("status", {})

        # Verify all status fields from GetCluster are populated
        assert status.get("identifier") is not None, "identifier missing"
        assert status.get("endpoint") is not None, "endpoint missing"
        assert status.get("status") is not None, "status missing"
        assert status.get("creationTime") is not None, "creationTime missing"

        encryption = status.get("encryptionDetails")
        assert encryption is not None, "encryptionDetails missing"
        assert encryption.get("encryptionStatus") is not None, \
            "encryptionStatus missing"
        assert encryption.get("encryptionType") is not None, \
            "encryptionType missing"

        # Verify ACK resource metadata has the ARN
        ack_metadata = status.get("ackResourceMetadata", {})
        assert ack_metadata.get("arn") is not None, "ARN missing from ackResourceMetadata"

        # Cross-check with AWS
        identifier = status["identifier"]
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None

        aws_endpoint = aws_cluster.get("endpoint")
        assert status["endpoint"] == aws_endpoint, \
            f"Endpoint mismatch: CR={status['endpoint']}, AWS={aws_endpoint}"

    def test_multi_region_cluster(self, dsql_client, multi_region_cluster):
        """Test that creating a Cluster with witnessRegion enters PENDING_SETUP.

        A cluster created with multiRegionProperties.witnessRegion but no
        linked peer clusters stays in PENDING_SETUP until bidirectional
        peering is established. This test verifies the cluster is created
        in AWS with the correct witnessRegion and the expected status.
        """
        (ref, cr) = multi_region_cluster

        # Give the controller time to create the cluster and reconcile
        time.sleep(CREATE_WAIT_AFTER_SECONDS)

        # The cluster should exist but be in PENDING_SETUP (not ACTIVE),
        # so Synced will be False. Poll the CR until status is populated.
        cr = None
        for _ in range(ACTIVE_WAIT_PERIODS):
            cr = k8s.get_resource(ref)
            status = cr.get("status", {})
            if status.get("identifier") is not None:
                break
            time.sleep(ACTIVE_WAIT_PERIOD_LENGTH)

        assert cr is not None
        status = cr.get("status", {})
        identifier = status.get("identifier")
        assert identifier is not None, "identifier not populated"

        # Verify the cluster exists in AWS with PENDING_SETUP status
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None, "Multi-region cluster not found in AWS"
        assert aws_cluster.get("status") in ("CREATING", "PENDING_SETUP"), \
            f"Expected CREATING or PENDING_SETUP, got {aws_cluster.get('status')}"

        # Verify multiRegionProperties in the AWS response
        multi_region_props = aws_cluster.get("multiRegionProperties")
        assert multi_region_props is not None, \
            "multiRegionProperties not found in AWS GetCluster response"

        witness_region = multi_region_props.get("witnessRegion")
        assert witness_region is not None, "witnessRegion not set in AWS"

        expected_witness = os.environ.get("WITNESS_REGION", "us-east-2")
        assert witness_region == expected_witness, \
            f"witnessRegion mismatch: expected '{expected_witness}', got '{witness_region}'"

    def test_deletion_protection_blocks_delete(self, dsql_client):
        """Test that CRD CEL validation blocks deletion when deletionProtectionEnabled is true.

        This test validates that the x-kubernetes-validations CEL rule on the
        CRD prevents deletion of a Cluster CR when spec.deletionProtectionEnabled
        is true, avoiding the deadlock described in community#2436.

        Flow:
        1. Create cluster (DSQL defaults deletionProtectionEnabled to true)
        2. Wait for ACTIVE and late-initialization of the field
        3. Attempt delete — expect rejection by CEL rule
        4. Patch deletionProtectionEnabled to false
        5. Wait for update to reconcile
        6. Delete again — expect success
        """
        resource_name = random_suffix_name("ack-dsql-dp", 24)

        replacements = REPLACEMENT_VALUES.copy()
        replacements["CLUSTER_NAME"] = resource_name

        resource_data = load_dsql_resource(
            "cluster",
            additional_replacements=replacements,
        )

        ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
            resource_name, namespace="default",
        )
        k8s.create_custom_resource(ref, resource_data)
        cr = k8s.wait_resource_consumed_by_controller(ref)
        assert cr is not None

        # Wait for ACTIVE so late-initialization populates deletionProtectionEnabled
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach ACTIVE before deletion protection test"
        condition.assert_synced(ref)

        # Verify deletionProtectionEnabled was late-initialized to true
        cr = k8s.get_resource(ref)
        dp_value = cr.get("spec", {}).get("deletionProtectionEnabled")
        assert dp_value is True, \
            f"Expected deletionProtectionEnabled=true after late-init, got {dp_value}"

        identifier = cr["status"]["identifier"]

        # Step 1: Attempt to delete — should be rejected by CEL validation
        delete_rejected = False
        try:
            k8s.delete_custom_resource(ref, 1, 5)
        except ApiException as e:
            if e.status in (403, 422):
                delete_rejected = True
                logging.info(f"Delete correctly rejected: {e.reason}")
            else:
                raise

        assert delete_rejected, \
            "Expected delete to be rejected by CEL validation when deletionProtectionEnabled=true"

        # Verify the resource still exists
        assert k8s.get_resource_exists(ref), \
            "Resource should still exist after rejected delete"

        # Step 2: Disable deletion protection
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        # Wait for the update to reconcile
        assert _wait_for_cluster_active(ref), \
            "Cluster did not return to Synced after disabling deletion protection"
        condition.assert_synced(ref)

        # Verify the field was updated
        cr = k8s.get_resource(ref)
        dp_value = cr.get("spec", {}).get("deletionProtectionEnabled")
        assert dp_value is False, \
            f"Expected deletionProtectionEnabled=false after patch, got {dp_value}"

        # Step 3: Delete again — should succeed now
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted, "Delete should succeed when deletionProtectionEnabled=false"

        # Wait for AWS deletion
        for _ in range(30):
            time.sleep(20)
            aws_cluster = _get_aws_cluster(dsql_client, identifier)
            if aws_cluster is None or aws_cluster.get("status") == "DELETED":
                return

        pytest.fail(
            f"Cluster {identifier} was not deleted from AWS after disabling protection"
        )

    def test_multi_region_cluster_peered(self, dsql_client):
        """Test multi-region cluster with bidirectional peering reaches ACTIVE.

        Creates a peer cluster in a different region via boto3, then creates
        an ACK-managed cluster linked to it. After establishing bidirectional
        peering, both clusters should reach ACTIVE.
        """
        # Configuration — controller region is where ACK runs
        controller_region = os.environ.get("AWS_REGION", "us-west-2")
        peer_region = os.environ.get("PEER_REGION", "us-east-1")
        witness_region = os.environ.get("WITNESS_REGION", "us-east-2")

        # Step 1: Create peer cluster in a different region via boto3
        peer_dsql = boto3.client("dsql", region_name=peer_region)
        peer_resp = peer_dsql.create_cluster(
            multiRegionProperties={"witnessRegion": witness_region},
            tags={"Environment": "testing", "ManagedBy": "ACK-e2e"},
        )
        peer_arn = peer_resp["arn"]
        peer_identifier = peer_resp["identifier"]
        logging.info(f"Created peer cluster {peer_identifier} in {peer_region}")

        try:
            # Wait for peer to reach PENDING_SETUP (it has no linked clusters yet)
            peer_cluster = _wait_for_aws_cluster_status(
                peer_dsql, peer_identifier, ("PENDING_SETUP",),
            )
            assert peer_cluster is not None, \
                f"Peer cluster did not reach PENDING_SETUP in {peer_region}"

            # Step 2: Create ACK-managed cluster with the peer ARN
            resource_name = random_suffix_name("ack-dsql-mrp", 24)
            replacements = REPLACEMENT_VALUES.copy()
            replacements["CLUSTER_NAME"] = resource_name
            replacements["WITNESS_REGION"] = witness_region
            replacements["LINKED_CLUSTER_ARN"] = peer_arn

            resource_data = load_dsql_resource(
                "cluster_multi_region_peered",
                additional_replacements=replacements,
            )

            ref = k8s.CustomResourceReference(
                CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
                resource_name, namespace="default",
            )
            k8s.create_custom_resource(ref, resource_data)
            cr = k8s.wait_resource_consumed_by_controller(ref)
            assert cr is not None

            # Wait for the ACK cluster identifier to be populated
            ack_identifier = None
            for _ in range(ACTIVE_WAIT_PERIODS):
                cr = k8s.get_resource(ref)
                ack_identifier = cr.get("status", {}).get("identifier")
                if ack_identifier is not None:
                    break
                time.sleep(ACTIVE_WAIT_PERIOD_LENGTH)
            assert ack_identifier is not None, "ACK cluster identifier not populated"

            # Get the ACK cluster ARN
            ack_arn = cr["status"].get("ackResourceMetadata", {}).get("arn", "")
            assert ack_arn != "", "ACK cluster ARN not populated"

            # Step 3: Update peer cluster to link back to the ACK cluster
            peer_dsql.update_cluster(
                identifier=peer_identifier,
                multiRegionProperties={
                    "witnessRegion": witness_region,
                    "clusters": [ack_arn],
                },
            )
            logging.info(f"Updated peer cluster {peer_identifier} with link to {ack_arn}")

            # Step 4: Wait for ACK-managed cluster to reach ACTIVE
            assert _wait_for_cluster_active(ref), \
                "ACK multi-region cluster did not reach ACTIVE after peering"
            condition.assert_synced(ref)

            # Verify the ACK cluster in AWS
            aws_cluster = _get_aws_cluster(dsql_client, ack_identifier)
            assert aws_cluster is not None
            assert aws_cluster.get("status") == "ACTIVE"

            multi_region_props = aws_cluster.get("multiRegionProperties")
            assert multi_region_props is not None
            assert multi_region_props.get("witnessRegion") == witness_region
            clusters_list = multi_region_props.get("clusters", [])
            assert peer_arn in clusters_list, \
                f"Peer ARN {peer_arn} not in clusters list: {clusters_list}"

            # Verify peer cluster also reached ACTIVE
            peer_cluster = _wait_for_aws_cluster_status(
                peer_dsql, peer_identifier, ("ACTIVE",),
            )
            assert peer_cluster is not None, \
                f"Peer cluster did not reach ACTIVE in {peer_region}"

        finally:
            # Teardown: disable deletion protection, delete ACK CR, then peer cluster
            try:
                updates = {"spec": {"deletionProtectionEnabled": False}}
                k8s.patch_custom_resource(ref, updates)
                time.sleep(UPDATE_WAIT_AFTER_SECONDS)
                _, deleted = k8s.delete_custom_resource(ref, 3, 10)
            except Exception:
                pass

            # Wait for ACK cluster to be deleted from AWS before deleting peer
            for _ in range(30):
                time.sleep(20)
                c = _get_aws_cluster(dsql_client, ack_identifier) if ack_identifier else None
                if c is None or c.get("status") == "DELETED":
                    break

            # Delete peer cluster
            try:
                peer_dsql.delete_cluster(identifier=peer_identifier)
                logging.info(f"Deleted peer cluster {peer_identifier}")
            except Exception as e:
                logging.warning(f"Failed to delete peer cluster: {e}")
