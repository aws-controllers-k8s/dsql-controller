// Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License"). You may
// not use this file except in compliance with the License. A copy of the
// License is located at
//
//     http://aws.amazon.com/apache2.0/
//
// or in the "license" file accompanying this file. This file is distributed
// on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
// express or implied. See the License for the specific language governing
// permissions and limitations under the License.

package cluster

import (
	"context"
	"errors"
	"fmt"

	ackcompare "github.com/aws-controllers-k8s/runtime/pkg/compare"
	ackrequeue "github.com/aws-controllers-k8s/runtime/pkg/requeue"
	svcsdk "github.com/aws/aws-sdk-go-v2/service/dsql"
	svcsdktypes "github.com/aws/aws-sdk-go-v2/service/dsql/types"

	svcapitypes "github.com/aws-controllers-k8s/dsql-controller/apis/v1alpha1"
	"github.com/aws-controllers-k8s/dsql-controller/pkg/sync"
)

// syncTags is exposed as a package-level variable so the generated update
// hook can call the shared tag-sync helper from pkg/sync.
var syncTags = sync.Tags

// transitionalStatuses are the cluster states during which the DSQL API
// rejects mutations. Updates issued while the cluster is in one of these
// states return a validation error, so we requeue instead.
var transitionalStatuses = map[string]struct{}{
	"CREATING":      {},
	"UPDATING":      {},
	"PENDING_SETUP": {},
}

// requeueIfTransitional returns an ackrequeue error if the latest cluster
// status is one of the transitional states during which DSQL rejects
// mutations. Returns nil if the cluster is in a stable state and the update
// can proceed.
func requeueIfTransitional(latest *resource) error {
	if latest.ko.Status.Status == nil {
		return nil
	}
	status := *latest.ko.Status.Status
	if _, ok := transitionalStatuses[status]; ok {
		return ackrequeue.NeededAfter(
			fmt.Errorf("cluster is in transitional state '%s', cannot update", status),
			ackrequeue.DefaultRequeueAfterDuration,
		)
	}
	return nil
}

// setResourcePolicy fetches the current cluster policy via the dedicated
// GetClusterPolicy API and populates ko.Spec.Policy with the current AWS
// value. Policy is not returned by GetCluster, so we fetch it separately
// in the read path. This function is read-only — policy mutations are
// handled in syncPolicy.
//
// A ResourceNotFoundException from GetClusterPolicy is normal when no
// policy is attached and is treated as ko.Spec.Policy = nil.
func setResourcePolicy(
	ctx context.Context,
	rm *resourceManager,
	ko *svcapitypes.Cluster,
) error {
	if ko.Status.Identifier == nil {
		return nil
	}
	resp, err := rm.sdkapi.GetClusterPolicy(ctx, &svcsdk.GetClusterPolicyInput{
		Identifier: ko.Status.Identifier,
	})
	rm.metrics.RecordAPICall("GET", "GetClusterPolicy", err)
	if err != nil {
		var notFound *svcsdktypes.ResourceNotFoundException
		if !errors.As(err, &notFound) {
			return err
		}
		// No policy attached — leave as nil so it matches a desired spec
		// that also has no policy (avoids a spurious nil vs "" delta).
		ko.Spec.Policy = nil
		return nil
	}
	if resp.Policy != nil {
		ko.Spec.Policy = resp.Policy
	} else {
		ko.Spec.Policy = nil
	}
	return nil
}

// syncPolicy reconciles the cluster policy by calling PutClusterPolicy when
// the desired policy is non-empty or DeleteClusterPolicy when the desired
// policy is empty/nil. Policy sync is handled in the update path (not in
// sdkFind) to keep the read path side-effect free.
//
// A ResourceNotFoundException from DeleteClusterPolicy is treated as
// success — the policy was already absent.
func syncPolicy(
	ctx context.Context,
	rm *resourceManager,
	desired *resource,
	latest *resource,
) error {
	desiredPolicy := ""
	if desired.ko.Spec.Policy != nil {
		desiredPolicy = *desired.ko.Spec.Policy
	}
	if desiredPolicy != "" {
		_, err := rm.sdkapi.PutClusterPolicy(ctx, &svcsdk.PutClusterPolicyInput{
			Identifier: latest.ko.Status.Identifier,
			Policy:     &desiredPolicy,
		})
		rm.metrics.RecordAPICall("UPDATE", "PutClusterPolicy", err)
		return err
	}
	_, err := rm.sdkapi.DeleteClusterPolicy(ctx, &svcsdk.DeleteClusterPolicyInput{
		Identifier: latest.ko.Status.Identifier,
	})
	rm.metrics.RecordAPICall("UPDATE", "DeleteClusterPolicy", err)
	if err != nil {
		var notFound *svcsdktypes.ResourceNotFoundException
		if errors.As(err, &notFound) {
			// Policy already absent — treat as success.
			return nil
		}
		return err
	}
	return nil
}

// customUpdate handles non-UpdateCluster mutations (tags and policy) and
// signals back to the caller whether the UpdateCluster API call should be
// skipped. Returns (skipUpdate, err): if skipUpdate is true and err is nil,
// the caller should return desired without invoking UpdateCluster.
func customUpdate(
	ctx context.Context,
	rm *resourceManager,
	desired *resource,
	latest *resource,
	delta *ackcompare.Delta,
) (skipUpdate bool, err error) {
	if err := requeueIfTransitional(latest); err != nil {
		return false, err
	}
	if delta.DifferentAt("Spec.Tags") {
		arn := (*string)(latest.ko.Status.ACKResourceMetadata.ARN)
		if err := syncTags(
			ctx,
			desired.ko.Spec.Tags, latest.ko.Spec.Tags,
			arn, convertToOrderedACKTags, rm.sdkapi, rm.metrics,
		); err != nil {
			return false, err
		}
	}
	if delta.DifferentAt("Spec.Policy") {
		if err := syncPolicy(ctx, rm, desired, latest); err != nil {
			return false, err
		}
	}
	// If only tags and/or policy changed, skip the UpdateCluster API call.
	if !delta.DifferentExcept("Spec.Tags", "Spec.Policy") {
		return true, nil
	}
	return false, nil
}
