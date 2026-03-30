	// Handle async cluster lifecycle states.
	// The ACK runtime automatically requeues when ResourceSynced is False.
	if ko.Status.Status != nil {
		switch *ko.Status.Status {
		case "CREATING", "UPDATING", "DELETING", "PENDING_SETUP", "PENDING_DELETE":
			ackcondition.SetSynced(&resource{ko}, corev1.ConditionFalse, nil, nil)
			return &resource{ko}, nil
		case "FAILED":
			msg := "Cluster is in FAILED state"
			ackcondition.SetTerminal(&resource{ko}, corev1.ConditionTrue, &msg, nil)
			return &resource{ko}, nil
		case "ACTIVE", "IDLE", "INACTIVE":
			ackcondition.SetSynced(&resource{ko}, corev1.ConditionTrue, nil, nil)
		}
	}

	// Sync policy from the dedicated GetClusterPolicy API.
	// Policy is a custom spec field managed separately from CreateCluster/UpdateCluster.
	if ko.Status.Identifier != nil {
		policyResp, err := rm.sdkapi.GetClusterPolicy(ctx, &svcsdk.GetClusterPolicyInput{
			Identifier: ko.Status.Identifier,
		})
		if err != nil {
			var notFound *svcsdktypes.ResourceNotFoundException
			if !errors.As(err, &notFound) {
				return nil, err
			}
			// ResourceNotFoundException means no policy is attached
			ko.Spec.Policy = nil
		} else if policyResp.Policy != nil {
			ko.Spec.Policy = policyResp.Policy
		}
	}
