	// Handle policy changes via dedicated PutClusterPolicy/DeleteClusterPolicy APIs.
	if delta.DifferentAt("Spec.Policy") {
		if desired.ko.Spec.Policy != nil && *desired.ko.Spec.Policy != "" {
			_, err = rm.sdkapi.PutClusterPolicy(ctx, &svcsdk.PutClusterPolicyInput{
				Identifier: latest.ko.Status.Identifier,
				Policy:     desired.ko.Spec.Policy,
			})
			if err != nil {
				return nil, err
			}
		} else {
			_, err = rm.sdkapi.DeleteClusterPolicy(ctx, &svcsdk.DeleteClusterPolicyInput{
				Identifier: latest.ko.Status.Identifier,
			})
			if err != nil {
				// Ignore ResourceNotFoundException — policy may already be gone
				var notFound *svcsdktypes.ResourceNotFoundException
				if !errors.As(err, &notFound) {
					return nil, err
				}
			}
		}
	}

	// Handle tag changes via TagResource/UntagResource APIs.
	if delta.DifferentAt("Spec.Tags") {
		desiredTags, _ := convertToOrderedACKTags(desired.ko.Spec.Tags)
		latestTags, _ := convertToOrderedACKTags(latest.ko.Spec.Tags)
		added, _, removed := ackcompare.GetTagsDifference(latestTags, desiredTags)
		// Remove keys from 'removed' that are also in 'added' (they are updates, not removals)
		for key := range removed {
			if _, ok := added[key]; ok {
				delete(removed, key)
			}
		}
		arn := (*string)(latest.ko.Status.ACKResourceMetadata.ARN)
		if len(removed) > 0 {
			removedKeys := make([]string, 0, len(removed))
			for key := range removed {
				removedKeys = append(removedKeys, key)
			}
			_, err = rm.sdkapi.UntagResource(ctx, &svcsdk.UntagResourceInput{
				ResourceArn: arn,
				TagKeys:     removedKeys,
			})
			rm.metrics.RecordAPICall("UPDATE", "UntagResource", err)
			if err != nil {
				return nil, err
			}
		}
		if len(added) > 0 {
			addedMap := make(map[string]string, len(added))
			for key, val := range added {
				addedMap[key] = val
			}
			_, err = rm.sdkapi.TagResource(ctx, &svcsdk.TagResourceInput{
				ResourceArn: arn,
				Tags:        addedMap,
			})
			rm.metrics.RecordAPICall("UPDATE", "TagResource", err)
			if err != nil {
				return nil, err
			}
		}
	}

	// If only policy and/or tags changed, skip the UpdateCluster API call.
	if !delta.DifferentExcept("Spec.Policy", "Spec.Tags") {
		return desired, nil
	}
