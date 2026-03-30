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

package property

import (
	"testing"

	ackv1alpha1 "github.com/aws-controllers-k8s/runtime/apis/core/v1alpha1"
	ackcondition "github.com/aws-controllers-k8s/runtime/pkg/condition"
	corev1 "k8s.io/api/core/v1"
	"pgregory.net/rapid"
)

// testConditionManager implements acktypes.ConditionManager for testing
// condition-setting logic without the unexported generated resource struct.
type testConditionManager struct {
	conditions []*ackv1alpha1.Condition
}

func (t *testConditionManager) Conditions() []*ackv1alpha1.Condition {
	return t.conditions
}

func (t *testConditionManager) ReplaceConditions(conditions []*ackv1alpha1.Condition) {
	t.conditions = conditions
}

func findCondition(conditions []*ackv1alpha1.Condition, condType ackv1alpha1.ConditionType) *ackv1alpha1.Condition {
	for _, c := range conditions {
		if c.Type == condType {
			return c
		}
	}
	return nil
}

// applyStatusConditionMapping replicates the status-to-condition mapping logic
// from dsql-controller/pkg/resource/cluster/sdk.go sdkFind function.
func applyStatusConditionMapping(cm *testConditionManager, status string) {
	switch status {
	case "CREATING", "UPDATING", "DELETING", "PENDING_SETUP", "PENDING_DELETE":
		ackcondition.SetSynced(cm, corev1.ConditionFalse, nil, nil)
	case "FAILED":
		msg := "Cluster is in FAILED state"
		ackcondition.SetTerminal(cm, corev1.ConditionTrue, &msg, nil)
	case "ACTIVE", "IDLE", "INACTIVE":
		ackcondition.SetSynced(cm, corev1.ConditionTrue, nil, nil)
	}
}

// TestProperty7_AsyncStatusToConditionMapping verifies that each cluster
// lifecycle status maps to the correct ACK condition.
//
// **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5**
func TestProperty7_AsyncStatusToConditionMapping(t *testing.T) {
	allStatuses := []string{
		"CREATING", "UPDATING", "DELETING", "PENDING_SETUP", "PENDING_DELETE",
		"ACTIVE", "IDLE", "INACTIVE",
		"FAILED",
	}
	transitionalStatuses := map[string]bool{
		"CREATING": true, "UPDATING": true, "DELETING": true,
		"PENDING_SETUP": true, "PENDING_DELETE": true,
	}
	activeStatuses := map[string]bool{
		"ACTIVE": true, "IDLE": true, "INACTIVE": true,
	}

	rapid.Check(t, func(t *rapid.T) {
		status := rapid.SampledFrom(allStatuses).Draw(t, "clusterStatus")
		cm := &testConditionManager{conditions: []*ackv1alpha1.Condition{}}

		applyStatusConditionMapping(cm, status)

		if transitionalStatuses[status] {
			synced := findCondition(cm.conditions, ackv1alpha1.ConditionTypeResourceSynced)
			if synced == nil {
				t.Fatalf("expected Synced condition for transitional status %q, got nil", status)
			}
			if synced.Status != corev1.ConditionFalse {
				t.Fatalf("expected Synced=False for transitional status %q, got %v", status, synced.Status)
			}
			terminal := findCondition(cm.conditions, ackv1alpha1.ConditionTypeTerminal)
			if terminal != nil {
				t.Fatalf("unexpected Terminal condition for transitional status %q", status)
			}
		} else if activeStatuses[status] {
			synced := findCondition(cm.conditions, ackv1alpha1.ConditionTypeResourceSynced)
			if synced == nil {
				t.Fatalf("expected Synced condition for active status %q, got nil", status)
			}
			if synced.Status != corev1.ConditionTrue {
				t.Fatalf("expected Synced=True for active status %q, got %v", status, synced.Status)
			}
			terminal := findCondition(cm.conditions, ackv1alpha1.ConditionTypeTerminal)
			if terminal != nil {
				t.Fatalf("unexpected Terminal condition for active status %q", status)
			}
		} else if status == "FAILED" {
			terminal := findCondition(cm.conditions, ackv1alpha1.ConditionTypeTerminal)
			if terminal == nil {
				t.Fatalf("expected Terminal condition for FAILED status, got nil")
			}
			if terminal.Status != corev1.ConditionTrue {
				t.Fatalf("expected Terminal=True for FAILED, got %v", terminal.Status)
			}
			if terminal.Message == nil || *terminal.Message == "" {
				t.Fatalf("expected Terminal message for FAILED status")
			}
			synced := findCondition(cm.conditions, ackv1alpha1.ConditionTypeResourceSynced)
			if synced != nil {
				t.Fatalf("unexpected Synced condition for FAILED status")
			}
		}
	})
}
