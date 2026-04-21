	// No custom post-create logic needed. synced:when in generator.yaml
	// handles the Synced condition based on cluster status. A newly created
	// cluster starts in CREATING status, which is not in the synced list
	// (ACTIVE, IDLE, INACTIVE), so the runtime automatically sets
	// Synced=False and requeues until the cluster reaches a stable state.
