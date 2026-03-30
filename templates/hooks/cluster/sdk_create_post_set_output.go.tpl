	// Cluster creation is asynchronous — the cluster starts in CREATING state.
	// Set ResourceSynced to False so the ACK runtime requeues until ACTIVE.
	ackcondition.SetSynced(&resource{ko}, corev1.ConditionFalse, nil, nil)
