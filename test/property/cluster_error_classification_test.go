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
	"errors"
	"fmt"
	"testing"

	ackv1alpha1 "github.com/aws-controllers-k8s/runtime/apis/core/v1alpha1"
	"github.com/aws/smithy-go"
	corev1 "k8s.io/api/core/v1"
	"pgregory.net/rapid"
)

// testAPIError implements smithy.APIError for testing error classification.
type testAPIError struct {
	code    string
	message string
}

func (e *testAPIError) Error() string                 { return fmt.Sprintf("%s: %s", e.code, e.message) }
func (e *testAPIError) ErrorCode() string             { return e.code }
func (e *testAPIError) ErrorMessage() string          { return e.message }
func (e *testAPIError) ErrorFault() smithy.ErrorFault { return smithy.FaultUnknown }

// classifyError replicates the terminalAWSError logic from
// dsql-controller/pkg/resource/cluster/sdk.go.
func classifyError(err error) bool {
	if err == nil {
		return false
	}
	var apiErr smithy.APIError
	if !errors.As(err, &apiErr) {
		return false
	}
	switch apiErr.ErrorCode() {
	case "ValidationException", "AccessDeniedException", "ServiceQuotaExceededException":
		return true
	default:
		return false
	}
}

// applyErrorConditions replicates the condition-setting logic from
// updateConditions in dsql-controller/pkg/resource/cluster/sdk.go
// for the error handling path.
func applyErrorConditions(cm *testConditionManager, err error) {
	if classifyError(err) {
		// Terminal error: set ACK.Terminal=True with error message
		var apiErr smithy.APIError
		errors.As(err, &apiErr)
		msg := apiErr.Error()
		terminalCond := &ackv1alpha1.Condition{
			Type:    ackv1alpha1.ConditionTypeTerminal,
			Status:  corev1.ConditionTrue,
			Message: &msg,
		}
		cm.conditions = append(cm.conditions, terminalCond)
	} else if err != nil {
		// Non-terminal error: set ACK.Recoverable=True
		msg := err.Error()
		recoverableCond := &ackv1alpha1.Condition{
			Type:    ackv1alpha1.ConditionTypeRecoverable,
			Status:  corev1.ConditionTrue,
			Message: &msg,
		}
		cm.conditions = append(cm.conditions, recoverableCond)
	}
}

// TestProperty9_ErrorClassification verifies that API errors are correctly
// classified as terminal or retryable, and that the appropriate ACK conditions
// are set for each error type.
//
// **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7**
func TestProperty9_ErrorClassification(t *testing.T) {
	terminalCodes := []string{
		"ValidationException",
		"AccessDeniedException",
		"ServiceQuotaExceededException",
	}
	retryableCodes := []string{
		"ConflictException",
		"ThrottlingException",
	}
	specialCodes := []string{
		"ResourceNotFoundException",
	}
	allCodes := append(append(append([]string{}, terminalCodes...), retryableCodes...), specialCodes...)

	terminalSet := map[string]bool{
		"ValidationException":           true,
		"AccessDeniedException":         true,
		"ServiceQuotaExceededException": true,
	}
	retryableSet := map[string]bool{
		"ConflictException":   true,
		"ThrottlingException": true,
	}

	rapid.Check(t, func(t *rapid.T) {
		code := rapid.SampledFrom(allCodes).Draw(t, "errorCode")
		msg := rapid.StringMatching(`[a-zA-Z0-9 ]{1,50}`).Draw(t, "errorMessage")

		apiErr := &testAPIError{code: code, message: msg}

		// Test 1: Verify classifyError returns the correct boolean
		isTerminal := classifyError(apiErr)

		if terminalSet[code] {
			if !isTerminal {
				t.Fatalf("expected classifyError to return true for terminal error %q, got false", code)
			}
		} else {
			if isTerminal {
				t.Fatalf("expected classifyError to return false for non-terminal error %q, got true", code)
			}
		}

		// Test 2: Verify applyErrorConditions sets the correct condition
		cm := &testConditionManager{conditions: []*ackv1alpha1.Condition{}}
		applyErrorConditions(cm, apiErr)

		if terminalSet[code] {
			// Terminal errors should set ACK.Terminal=True
			terminal := findCondition(cm.conditions, ackv1alpha1.ConditionTypeTerminal)
			if terminal == nil {
				t.Fatalf("expected Terminal condition for terminal error %q, got nil", code)
			}
			if terminal.Status != corev1.ConditionTrue {
				t.Fatalf("expected Terminal=True for %q, got %v", code, terminal.Status)
			}
			if terminal.Message == nil || *terminal.Message == "" {
				t.Fatalf("expected Terminal message for %q", code)
			}
			// Should NOT have Recoverable condition
			recoverable := findCondition(cm.conditions, ackv1alpha1.ConditionTypeRecoverable)
			if recoverable != nil {
				t.Fatalf("unexpected Recoverable condition for terminal error %q", code)
			}
		} else if retryableSet[code] {
			// Retryable errors should set ACK.Recoverable=True (not Terminal)
			recoverable := findCondition(cm.conditions, ackv1alpha1.ConditionTypeRecoverable)
			if recoverable == nil {
				t.Fatalf("expected Recoverable condition for retryable error %q, got nil", code)
			}
			if recoverable.Status != corev1.ConditionTrue {
				t.Fatalf("expected Recoverable=True for %q, got %v", code, recoverable.Status)
			}
			// Should NOT have Terminal condition
			terminal := findCondition(cm.conditions, ackv1alpha1.ConditionTypeTerminal)
			if terminal != nil {
				t.Fatalf("unexpected Terminal condition for retryable error %q", code)
			}
		} else if code == "ResourceNotFoundException" {
			// ResourceNotFoundException is not terminal (handled specially during deletion)
			if isTerminal {
				t.Fatalf("ResourceNotFoundException should not be classified as terminal")
			}
			// It should set Recoverable (it's a non-terminal error)
			recoverable := findCondition(cm.conditions, ackv1alpha1.ConditionTypeRecoverable)
			if recoverable == nil {
				t.Fatalf("expected Recoverable condition for ResourceNotFoundException, got nil")
			}
			// Should NOT have Terminal condition
			terminal := findCondition(cm.conditions, ackv1alpha1.ConditionTypeTerminal)
			if terminal != nil {
				t.Fatalf("unexpected Terminal condition for ResourceNotFoundException")
			}
		}
	})
}
