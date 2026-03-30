	// Guard: Do not attempt updates while the cluster is in a transitional
	// state. The DSQL API will reject mutations during these phases, so we
	// requeue and wait for the cluster to reach a stable state.
	if latest.ko.Status.Status != nil {
		latestStatus := *latest.ko.Status.Status
		if latestStatus == "CREATING" || latestStatus == "UPDATING" || latestStatus == "PENDING_SETUP" {
			return nil, ackrequeue.NeededAfter(
				fmt.Errorf("cluster is in transitional state '%s', cannot update", latestStatus),
				ackrequeue.DefaultRequeueAfterDuration,
			)
		}
	}

	// Handle tag changes via TagResource/UntagResource APIs.
	if delta.DifferentAt("Spec.Tags") {
		arn := (*string)(latest.ko.Status.ACKResourceMetadata.ARN)
		err = syncTags(
			ctx,
			desired.ko.Spec.Tags, latest.ko.Spec.Tags,
			arn, convertToOrderedACKTags, rm.sdkapi, rm.metrics,
		)
		if err != nil {
			return nil, err
		}
	}

	// Handle policy changes via PutClusterPolicy/DeleteClusterPolicy APIs.
	// Policy sync is handled here in the update path (not in sdkFind) to
	// keep the read path side-effect free.
	if delta.DifferentAt("Spec.Policy") {
		desiredPolicy := ""
		if desired.ko.Spec.Policy != nil {
			desiredPolicy = *desired.ko.Spec.Policy
		}
		if desiredPolicy != "" {
			_, err = rm.sdkapi.PutClusterPolicy(ctx, &svcsdk.PutClusterPolicyInput{
				Identifier: latest.ko.Status.Identifier,
				Policy:     &desiredPolicy,
			})
			if err != nil {
				return nil, err
			}
		} else {
			// Desired policy is empty — remove the existing policy.
			_, err = rm.sdkapi.DeleteClusterPolicy(ctx, &svcsdk.DeleteClusterPolicyInput{
				Identifier: latest.ko.Status.Identifier,
			})
			if err != nil {
				var notFound *svcsdktypes.ResourceNotFoundException
				if !errors.As(err, &notFound) {
					return nil, err
				}
				// ResourceNotFoundException means no policy exists — treat as success.
			}
		}
	}

	// If only tags and/or policy changed, skip the UpdateCluster API call.
	if !delta.DifferentExcept("Spec.Tags", "Spec.Policy") {
		return desired, nil
	}
