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
- Create with optional fields (deletionProtectionEnabled, tags)
- Wait for ACTIVE status
- Verify status fields populated (endpoint, encryptionDetails, identifier)
- Add policy, verify PutResourcePolicy called
- Update deletionProtectionEnabled, wait for ACTIVE
- Remove policy, verify DeleteResourcePolicy called
- Delete Cluster, verify cleanup
"""

import json
import pytest
import time
import logging

from acktest.resources import random_suffix_name
from acktest.k8s import resource as k8s
from acktest.k8s import condition
from acktest import tags
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

SAMPLE_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": "*"},
            "Action": "dsql:DbConnectAdmin",
            "Resource": "*",
        }
    ],
})


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
        return dsql_client.get_cluster(Identifier=identifier)
    except dsql_client.exceptions.ResourceNotFoundException:
        return None


def _get_aws_resource_policy(dsql_client, cluster_arn):
    """Get the resource policy for a cluster."""
    try:
        resp = dsql_client.get_resource_policy(ResourceArn=cluster_arn)
        return resp.get("Policy") or resp.get("policy")
    except dsql_client.exceptions.ResourceNotFoundException:
        return None


@pytest.fixture(scope="module")
def simple_cluster(dsql_client):
    """Create a simple Cluster with tags and deletionProtection disabled."""
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

    # Teardown: ensure deletion protection is off, then delete
    try:
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)
    except Exception:
        pass

    try:
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

    # Teardown
    try:
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)
    except Exception:
        pass

    try:
        _, deleted = k8s.delete_custom_resource(ref, 3, 10)
        assert deleted
    except Exception:
        pass


@service_marker
@pytest.mark.canary
class TestCluster:
    """E2E tests for the DSQL Cluster resource lifecycle."""

    def test_create_and_wait_for_active(self, dsql_client, simple_cluster):
        """Test that creating a Cluster CR invokes CreateCluster and reaches ACTIVE.

        Validates: Requirements 2.1, 2.2, 2.5, 2.7, 2.8, 6.1, 6.4
        """
        (ref, cr) = simple_cluster

        # Wait for the cluster to become ACTIVE
        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach ACTIVE status (ACK.ResourceSynced=True)"

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
        assert aws_cluster.get("Status") == "ACTIVE" or aws_cluster.get("status") == "ACTIVE"

    def test_verify_tags_on_create(self, dsql_client, cluster_with_tags):
        """Test that tags specified at creation are applied to the cluster.

        Validates: Requirements 2.5, 9.1
        """
        (ref, cr) = cluster_with_tags

        assert _wait_for_cluster_active(ref), \
            "Cluster did not reach ACTIVE status"

        cr = k8s.get_resource(ref)
        identifier = cr["status"]["identifier"]

        # Verify tags in AWS
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None

        # Get tags from the AWS response
        aws_tags = aws_cluster.get("Tags") or aws_cluster.get("tags") or {}

        expected_tags = {
            "Environment": "testing",
            "Team": "platform",
            "ManagedBy": "ACK",
        }

        for key, value in expected_tags.items():
            assert key in aws_tags, f"Tag '{key}' not found in AWS tags"
            assert aws_tags[key] == value, \
                f"Tag '{key}' expected '{value}', got '{aws_tags[key]}'"

    def test_add_policy(self, dsql_client, simple_cluster):
        """Test that adding a policy to the spec invokes PutResourcePolicy.

        Validates: Requirements 4.4, 3.6
        """
        (ref, cr) = simple_cluster

        # Ensure cluster is ACTIVE first
        assert _wait_for_cluster_active(ref)

        cr = k8s.get_resource(ref)
        arn = cr["status"].get("ackResourceMetadata", {}).get("arn")
        assert arn is not None, "Cluster ARN not available"

        # Add policy to spec
        updates = {"spec": {"policy": SAMPLE_POLICY}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        # Wait for sync
        assert _wait_for_cluster_active(ref)

        # Verify policy was applied in AWS
        aws_policy = _get_aws_resource_policy(dsql_client, arn)
        assert aws_policy is not None, "Policy not found on cluster"

        # Verify the policy content matches (compare as parsed JSON)
        if isinstance(aws_policy, str):
            aws_policy_parsed = json.loads(aws_policy)
        else:
            aws_policy_parsed = aws_policy
        expected_policy_parsed = json.loads(SAMPLE_POLICY)
        assert aws_policy_parsed == expected_policy_parsed, \
            "Policy content does not match"

        # Verify the policy is reflected back in the CR spec
        cr = k8s.get_resource(ref)
        cr_policy = cr.get("spec", {}).get("policy")
        assert cr_policy is not None, "Policy not synced back to CR spec"

    def test_update_deletion_protection(self, dsql_client, simple_cluster):
        """Test that updating deletionProtectionEnabled invokes UpdateCluster.

        Validates: Requirements 4.1
        """
        (ref, cr) = simple_cluster

        assert _wait_for_cluster_active(ref)

        cr = k8s.get_resource(ref)
        identifier = cr["status"]["identifier"]

        # Enable deletion protection
        updates = {"spec": {"deletionProtectionEnabled": True}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        # Wait for the update to complete
        assert _wait_for_cluster_active(ref), \
            "Cluster did not return to ACTIVE after update"

        # Verify in AWS
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None
        dp = aws_cluster.get("DeletionProtectionEnabled") or \
             aws_cluster.get("deletionProtectionEnabled")
        assert dp is True, "DeletionProtection not enabled in AWS"

        # Disable deletion protection (needed for cleanup)
        updates = {"spec": {"deletionProtectionEnabled": False}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        assert _wait_for_cluster_active(ref)

        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        dp = aws_cluster.get("DeletionProtectionEnabled") or \
             aws_cluster.get("deletionProtectionEnabled")
        assert dp is False, "DeletionProtection not disabled in AWS"

    def test_remove_policy(self, dsql_client, simple_cluster):
        """Test that removing the policy from spec invokes DeleteResourcePolicy.

        Validates: Requirements 4.5
        """
        (ref, cr) = simple_cluster

        assert _wait_for_cluster_active(ref)

        cr = k8s.get_resource(ref)
        arn = cr["status"].get("ackResourceMetadata", {}).get("arn")
        assert arn is not None

        # Ensure a policy is attached first (may already be from test_add_policy)
        current_policy = _get_aws_resource_policy(dsql_client, arn)
        if current_policy is None:
            updates = {"spec": {"policy": SAMPLE_POLICY}}
            k8s.patch_custom_resource(ref, updates)
            time.sleep(UPDATE_WAIT_AFTER_SECONDS)
            assert _wait_for_cluster_active(ref)

        # Remove policy by setting to empty string
        updates = {"spec": {"policy": ""}}
        k8s.patch_custom_resource(ref, updates)
        time.sleep(UPDATE_WAIT_AFTER_SECONDS)

        assert _wait_for_cluster_active(ref)

        # Verify policy was removed in AWS
        aws_policy = _get_aws_resource_policy(dsql_client, arn)
        assert aws_policy is None, "Policy still attached after removal"

    def test_update_tags(self, dsql_client, cluster_with_tags):
        """Test that modifying tags invokes TagResource/UntagResource.

        Validates: Requirements 4.6, 9.2, 9.3
        """
        (ref, cr) = cluster_with_tags

        assert _wait_for_cluster_active(ref)

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

        assert _wait_for_cluster_active(ref)

        # Verify tags in AWS
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        aws_tags = aws_cluster.get("Tags") or aws_cluster.get("tags") or {}

        assert aws_tags.get("Environment") == "staging", \
            f"Expected 'staging', got '{aws_tags.get('Environment')}'"
        assert aws_tags.get("NewTag") == "new-value", \
            "New tag not added"
        assert "ManagedBy" not in aws_tags or aws_tags.get("ManagedBy") is None, \
            "Removed tag still present"

    def test_delete_cluster(self, dsql_client):
        """Test that deleting the CR invokes DeleteCluster and cleans up.

        Validates: Requirements 5.1, 5.2
        """
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

        cr = k8s.get_resource(ref)
        identifier = cr["status"]["identifier"]

        # Verify cluster exists in AWS
        aws_cluster = _get_aws_cluster(dsql_client, identifier)
        assert aws_cluster is not None

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
            cluster_status = aws_cluster.get("Status") or aws_cluster.get("status")
            if cluster_status == "DELETED":
                return

        pytest.fail(
            f"Cluster {identifier} was not deleted from AWS after "
            f"{max_attempts * wait_seconds} seconds"
        )

    def test_status_fields_populated(self, dsql_client, simple_cluster):
        """Test that all read-only status fields are populated after sync.

        Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
        """
        (ref, cr) = simple_cluster

        assert _wait_for_cluster_active(ref)

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

        aws_endpoint = aws_cluster.get("Endpoint") or aws_cluster.get("endpoint")
        assert status["endpoint"] == aws_endpoint, \
            f"Endpoint mismatch: CR={status['endpoint']}, AWS={aws_endpoint}"
