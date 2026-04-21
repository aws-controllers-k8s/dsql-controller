
	// Read the current cluster policy via the dedicated GetClusterPolicy API.
	// Policy is not returned by GetCluster, so we fetch it separately and
	// populate ko.Spec.Policy with the current AWS value. This hook is
	// read-only — policy mutations are handled in the update hook.
	if ko.Status.Identifier != nil {
		policyResp, policyErr := rm.sdkapi.GetClusterPolicy(ctx, &svcsdk.GetClusterPolicyInput{
			Identifier: ko.Status.Identifier,
		})
		if policyErr != nil {
			var notFound *svcsdktypes.ResourceNotFoundException
			if !errors.As(policyErr, &notFound) {
				return nil, policyErr
			}
			// No policy attached — leave as nil so it matches a desired spec
			// that also has no policy (avoids a spurious nil vs "" delta).
			ko.Spec.Policy = nil
		} else if policyResp.Policy != nil {
			ko.Spec.Policy = policyResp.Policy
		} else {
			ko.Spec.Policy = nil
		}
	}
