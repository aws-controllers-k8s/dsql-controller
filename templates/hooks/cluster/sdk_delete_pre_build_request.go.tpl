	// Note: Users can annotate their Cluster CR with
	// services.k8s.aws/deletion-policy: retain to prevent the controller from
	// deleting the AWS resource when the CR is deleted.
	// Users must set spec.deletionProtectionEnabled to false before deleting
	// the CR, otherwise the DSQL API will reject the DeleteCluster call.
