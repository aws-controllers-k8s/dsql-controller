
	skipUpdate, err := customUpdate(ctx, rm, desired, latest, delta)
	if err != nil {
		return nil, err
	}
	if skipUpdate {
		return desired, nil
	}

