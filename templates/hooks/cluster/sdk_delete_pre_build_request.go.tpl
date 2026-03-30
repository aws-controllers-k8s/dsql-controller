	// Disable deletion protection before deleting the cluster.
	// The DSQL API defaults deletionProtectionEnabled to true, and since
	// we've removed this field from the CRD to avoid a known deadlock
	// (see https://github.com/aws-controllers-k8s/community/issues/2436),
	// the controller must disable it before deletion can succeed.
	// Users who want to protect clusters from accidental deletion should
	// use the ACK deletion-policy: retain annotation instead.
	if r.ko.Status.Identifier != nil {
		updateInput := &svcsdk.UpdateClusterInput{
			Identifier:                  r.ko.Status.Identifier,
			DeletionProtectionEnabled:   aws.Bool(false),
		}
		_, err = rm.sdkapi.UpdateCluster(ctx, updateInput)
		rm.metrics.RecordAPICall("UPDATE", "UpdateCluster", err)
		if err != nil {
			return nil, err
		}
	}
