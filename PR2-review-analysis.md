# PR #2 Review Analysis — dsql-controller "Add cluster resource"

Reviewer: **knottnt** (single review, 22 inline comments)

---

## Comment 1: Remove AccessDeniedException and ServiceQuotaExceededException from terminal_codes

**File:** `generator.yaml` (terminal_codes section)

**Original comment:**
> `AccessDenied` and `ServiceQuotaExceedException` generally shouldn't be included as terminal error codes. The reason being that the can be resolved out of band from ACK (either by updating the IAM permissions or increasing quotas). Terminal codes are for errors that indicate the controller can't proceed until the ACK resource's Spec has been updated.

**Verdict:** ✅ Agree — valid and important feedback

The reviewer is correct. `AccessDeniedException` and `ServiceQuotaExceededException` are resolvable out-of-band (fix IAM permissions or request quota increase) without changing the CR spec. Terminal codes should only be errors that require a spec change to fix. `ValidationException` is fine to keep.

**Suggested response:**
> Good catch — you're right that these are recoverable without spec changes. I'll remove `AccessDeniedException` and `ServiceQuotaExceededException` from `terminal_codes`, keeping only `ValidationException`.

---

## Comment 2: Question about multiRegionProperties.clusters field

**File:** `config/crd/bases/dsql.services.k8s.aws_clusters.yaml` (line ~91)

**Original comment:**
> Q: How does this parameter work? From the description it sounds like this field references a set of DSQL clusters.

**Verdict:** ℹ️ Informational question — needs a clear answer

The reviewer asks how `multiRegionProperties.clusters` works. This is a list of ARNs of linked DSQL clusters in a multi-region setup. It's part of the DSQL CreateCluster API's `MultiRegionProperties` shape.

**Suggested response:**
> `multiRegionProperties.clusters` is a list of ARNs for linked clusters in a multi-region DSQL deployment. When creating a multi-region cluster, you specify the ARNs of the peer clusters that should be linked together, along with a `witnessRegion`. This comes directly from the DSQL API's `MultiRegionProperties` shape.

---

## Comment 3: status.arn is duplicate of status.ackResourceMetadata.arn

**File:** `config/crd/bases/dsql.services.k8s.aws_clusters.yaml` (line 139)

**Original comment:**
> This looks like it could be a duplicate of `status.ackResourceMetadata.arn`.

**Verdict:** ✅ Agree — this is indeed redundant

The CRD has both `status.arn` and `status.ackResourceMetadata.arn`. ACK already stores the ARN in `ackResourceMetadata.ARN` by default. The separate `status.arn` field in generator.yaml (`ARN: is_read_only: true, from: GetCluster.Arn`) creates a duplicate. We should remove the custom `ARN` field config and let ACK handle it via `ackResourceMetadata`.

**Suggested response:**
> You're right, this is a duplicate. The ARN is already stored in `status.ackResourceMetadata.arn` by the ACK runtime. I'll remove the custom `ARN` field from `generator.yaml` to avoid the redundancy.

---

## Comment 4: Use `synced:when` instead of manual SetSynced in read_one hook

**File:** `pkg/resource/cluster/sdk.go` (line 162, sdkFind status handling)

**Original comment:**
> This logic looks like would be covered by the `synced:when` generator.yaml config. That configuration allows you to defined a set of states that indicate when the resource can be considered "Synced".
>
> https://github.com/aws-controllers-k8s/code-generator/blob/a9e2ceaadfc00a742e2ea2b6d6c68348f03e52a5/pkg/config/resource.go#L148

**Verdict:** ✅ Agree — the code-generator has built-in support for this

The reviewer points to the `synced:when` config in `ResourceConfig.Synced` which lets you declaratively specify which status values mean "synced". This would replace the manual `switch` statement in `sdk_read_one_post_set_output.go.tpl` that sets `ResourceSynced` based on cluster status. Using the declarative config is cleaner and more maintainable.

**Suggested response:**
> Great suggestion. I'll add a `synced:when` configuration to `generator.yaml` to declaratively define the synced states (ACTIVE, IDLE, INACTIVE) instead of the manual hook logic. This will also cover the post-create SetSynced call (Comment 10).

---

## Comment 5: Don't set Terminal condition for FAILED state

**File:** `pkg/resource/cluster/sdk.go` (line 167, FAILED case)

**Original comment:**
> I don't think we usually set a Terminal condition for these failed states. A terminal condition usually indicates an issue in the ACK resource's Spec whereas a FAILED state may indicate something went wrong the AWS side.

**Verdict:** ✅ Agree — correct ACK semantics

The reviewer is right. A `FAILED` cluster status indicates something went wrong on the AWS side, not necessarily a problem with the user's spec. Setting `Terminal=True` would stop reconciliation permanently, preventing recovery if AWS resolves the issue. The resource should remain in a non-synced state and keep retrying.

**Suggested response:**
> Agreed. A FAILED status from AWS doesn't necessarily mean the spec is wrong — it could be a transient AWS-side issue. I'll remove the Terminal condition for FAILED and instead keep it as not-synced so the controller continues to poll.

---

## Comment 6: Policy sync should not mutate in sdkFind — move to sdkUpdate

**File:** `pkg/resource/cluster/sdk.go` (lines 176-177, policy sync in sdkFind)

**Original comment:**
> Mutating the AWS resource in the sdkFind operation is pretty unexpected behavior. I'd recommend just calling `GetClusterPolicy` to retrieve the latest value here. The comparison can then be performed in delta.go and finally any needed update applied in `sdkUpdate`.

**Verdict:** ✅ Agree — this is an architectural issue

Performing writes (PutClusterPolicy/DeleteClusterPolicy) inside `sdkFind` is a significant anti-pattern. `sdkFind` should be read-only — it reads the current state of the AWS resource. The correct approach is:
1. In `sdkFind` (read_one hook): call `GetClusterPolicy` and set `ko.Spec.Policy` to the current value from AWS
2. Let the ACK delta comparison detect the difference
3. In `sdkUpdate`: call `PutClusterPolicy` or `DeleteClusterPolicy` as needed

This also means removing `compare.is_ignored: true` from the Policy field so the delta system can detect changes.

**Suggested response:**
> You're absolutely right — mutating AWS state in sdkFind is unexpected behavior. I'll refactor to:
> 1. Read the current policy in the read_one hook and populate `ko.Spec.Policy`
> 2. Remove `compare.is_ignored` from Policy so delta detection works
> 3. Handle PutClusterPolicy/DeleteClusterPolicy in the update hook

---

## Comment 7: Use `is_iam_policy` for policy comparison instead of string comparison

**File:** `pkg/resource/cluster/sdk.go` (line 200, `desiredPolicy != currentPolicy`)

**Original comment:**
> This is more for the delta.go check, but since this is a policy document it could be a good fit for our new `is_iam_policy` config to use a built-in policy doc comparison. Performing a directly string comparison has often lead to whitespace or field ordering differences causing unnecessary deltas in other resources.
>
> https://github.com/aws-controllers-k8s/code-generator/blob/a9e2ceaadfc00a742e2ea2b6d6c68348f03e52a5/pkg/config/field.go#L430

**Verdict:** ✅ Agree — important for correctness

Direct string comparison of JSON policy documents is fragile. Whitespace differences, field ordering, and Action/Resource as string vs array can cause false positives. The code-generator now has `is_iam_policy` field config that uses semantic IAM policy comparison. This should be used for the Policy field.

**Suggested response:**
> Great point about the `is_iam_policy` config. Policy documents are JSON and string comparison will cause unnecessary updates due to whitespace/ordering differences. I'll add `is_iam_policy: true` to the Policy field config in generator.yaml. This pairs well with moving the comparison to the delta path (Comment 6).

---

## Comment 8: Policy sync code belongs in sdkUpdate (reinforces Comment 6)

**File:** `pkg/resource/cluster/sdk.go` (lines 201-220)

**Original comment:**
> This sync code should be handled in sdkUpdate.

**Verdict:** ✅ Agree — same as Comment 6, reinforcing the point

This is a follow-up to Comment 6, specifically calling out the Put/Delete policy block. The entire policy mutation block should move to `sdkUpdate`.

**Suggested response:**
> Agreed, will address together with Comment 6.

---

## Comment 9: Policy should be included in CreateCluster call

**File:** `pkg/resource/cluster/sdk.go` (line 350, newCreateRequestPayload)

**Original comment:**
> Should Policy be applied here? I see that it is present in the [CreateCluster](https://docs.aws.amazon.com/aurora-dsql/latest/APIReference/API_CreateCluster.html#auroradsql-CreateCluster-request-policy) operation.

**Verdict:** ✅ Agree — the DSQL CreateCluster API supports Policy

The reviewer correctly notes that `CreateCluster` accepts a `Policy` parameter. Currently the Policy field is in `ignore.field_paths` (`CreateClusterInput.Policy`), which means it's excluded from the create payload. Since we're refactoring policy handling anyway, we should remove it from the ignore list and include it in the create request.

**Suggested response:**
> Good catch. The DSQL CreateCluster API does accept Policy. I'll remove `CreateClusterInput.Policy` from `ignore.field_paths` so the code generator includes it in the create payload. The `BypassPolicyLockoutSafetyCheck` can stay ignored.

---

## Comment 10: Use `synced:when` for post-create SetSynced (same as Comment 4)

**File:** `templates/hooks/cluster/sdk_create_post_set_output.go.tpl`

**Original comment:**
> I think this can be covered by synced:when as well.

**Verdict:** ✅ Agree — covered by `synced:when`

If we add `synced:when` config (Comment 4), the code generator will automatically handle setting ResourceSynced=False after create when the status is CREATING. This hook template becomes unnecessary.

**Suggested response:**
> Agreed — the `synced:when` config will handle this automatically. I'll remove this hook template once that's in place.

---

## Comment 11: Question about UpdateCluster behavior during UPDATING/PENDING_SETUP

**File:** `pkg/resource/cluster/sdk.go` (line 381, sdkUpdate)

**Original comment:**
> Q: does UpdateCluster allow clusters in an `UPDATING` or `PENDING_SETUP` status to be updated or does it return a validation error? If it's the later we'll need to check the status of the resource and requeue if we need to wait.

**Verdict:** ℹ️ Valid question — needs investigation and likely a guard

The reviewer asks whether DSQL's UpdateCluster API rejects calls when the cluster is in UPDATING or PENDING_SETUP state. If it returns a validation error, we need to check the status in `sdk_update_pre_build_request` and requeue if the cluster isn't in a stable state. This is a common pattern in ACK controllers for async resources.

**Suggested response:**
> Good question. I'll verify the DSQL API behavior. If UpdateCluster rejects calls during transitional states, I'll add a status check in the update pre-build hook to requeue with `ackrequeue.NeededAfter()` when the cluster is in CREATING/UPDATING/PENDING_SETUP state.

---

## Comment 12: Deletion protection note — mention ACK deletion-policy as alternative

**File:** `templates/hooks/cluster/sdk_delete_pre_build_request.go.tpl`

**Original comment:**
> Just to note an alternative solution. The user could also set the [ACK deletion-policy](https://aws-controllers-k8s.github.io/docs/guides/deletion-policy) to `retain` to avoid the delete call.

**Verdict:** ℹ️ Informational — good to acknowledge in docs

The reviewer notes that users can also use ACK's `deletion-policy: retain` annotation to prevent the delete call entirely, as an alternative to DSQL's deletion protection. This is a documentation improvement, not a code change.

**Suggested response:**
> Good point — I'll update the comment to mention the ACK `deletion-policy: retain` annotation as an alternative approach for users who want to prevent accidental deletion.

---

## Comment 13: Remove "Validates: Requirements X.X" references from tests

**File:** `test/e2e/tests/test_cluster.py` (line 192)

**Original comment:**
> These requirements don't appear to be referencing anything an can probably be removed.

**Verdict:** ✅ Agree — these references are dangling

The requirement numbers (e.g., "Requirements 2.1, 2.2, 2.5") don't reference any document in the PR. They appear to be from an internal spec/design doc that isn't part of the repo. Remove them to avoid confusion.

**Suggested response:**
> You're right, these are leftover references from the design spec and don't belong in the test code. I'll remove them.

---

## Comment 14: Use `condition.assert_synced` from acktest library

**File:** `test/e2e/tests/test_cluster.py` (line 201)

**Original comment:**
> In addition to checking the status fields we should also verify that the ACK resource reached a Synced state. [condition.assert_synced](https://github.com/aws-controllers-k8s/test-infra/blob/b710f5be74ca36bfa687f821b0d0a48e87059a17/src/acktest/k8s/condition.py#L95) from our acktest library could be a good fit here.

**Verdict:** ✅ Agree — use the standard test library

The acktest library provides `condition.assert_synced()` for verifying the Synced condition. Using it is more idiomatic and consistent with other ACK controllers' tests.

**Suggested response:**
> Will add `condition.assert_synced(ref)` after waiting for ACTIVE to verify the resource reached a proper Synced state.

---

## Comment 15: Validate ACK system tags using acktest.tags library

**File:** `test/e2e/tests/test_cluster.py` (line 247)

**Original comment:**
> We'll also want to validate that the ACK system tags have been applied to the AWS resource. We also have some test library functions that can help here.
>
> https://github.com/aws-controllers-k8s/test-infra/blob/main/src/acktest/tags.py

**Verdict:** ✅ Agree — ACK system tags should be verified

ACK automatically adds system tags (like `services.k8s.aws/controller-version`, etc.) to resources. The test should verify these are present using the `acktest.tags` library functions.

**Suggested response:**
> Good call. I'll use the `acktest.tags` library to validate that ACK system tags are applied alongside the user-specified tags.

---

## Comment 16 & 16b: Use boto3/acktest for account ID instead of ARN parsing

**File:** `test/e2e/tests/test_cluster.py` (line 269)

**Original comment (16):**
> I believe boto3 also provides some built-in functionality for getting the account id.

**Original comment (16b, reply):**
> Actually we also have a library function for this as well. Although you'll need to convert it to a string.
>
> https://github.com/aws-controllers-k8s/test-infra/blob/b710f5be74ca36bfa687f821b0d0a48e87059a17/src/acktest/aws/identity.py#L19

**Verdict:** ✅ Agree — cleaner and more reliable

Parsing the account ID from an ARN string is fragile. The acktest library provides `acktest.aws.identity.get_account_id()` for this purpose.

**Suggested response:**
> Will switch to using `acktest.aws.identity.get_account_id()` instead of ARN string parsing.

---

## Comment 17: Validate Synced state after updates

**File:** `test/e2e/tests/test_cluster.py` (line 276)

**Original comment:**
> nit: after performing an update we'll also want to validate that the ACK resource returned to a Synced state.

**Verdict:** ✅ Agree — standard test practice

After patching a resource, the test should wait for and verify that the resource returns to a Synced state. This confirms the controller successfully reconciled the change.

**Suggested response:**
> Will add Synced condition verification after each update operation.

---

## Comment 18: Replace manual polling with Synced condition wait

**File:** `test/e2e/tests/test_cluster.py` (lines 279-284)

**Original comment:**
> This kind of polling likely could replaced by waiting the the synced condition on the resource or waiting for the cluster to become active.

**Verdict:** ✅ Agree — simplifies test code

The manual retry loop polling AWS for the policy can be replaced by waiting for the ACK resource to reach Synced state. Once synced, the policy should be applied. Then verify via a single AWS API call.

**Suggested response:**
> Agreed — I'll replace the polling loop with `_wait_for_cluster_active(ref)` / `condition.assert_synced(ref)` and then verify the AWS state once.

---

## Comment 19: Add multi-region cluster test coverage

**File:** `test/e2e/tests/test_cluster.py` (file-level comment)

**Original comment:**
> The test coverage is really good here! One additional test case that would be good cover though would be multi-region clusters. It seems like that might be an interesting case for the controller that we'll want to make sure we can handle.

**Verdict:** ✅ Agree — important gap in test coverage

Multi-region is a key DSQL feature with interesting edge cases (cross-region coordination, witness region, linked cluster ARNs). A test for creating a multi-region cluster would significantly improve confidence.

**Suggested response:**
> Good point. I'll add a test case for multi-region cluster creation that exercises the `multiRegionProperties` field with `witnessRegion` and linked `clusters`. This may require test infrastructure in multiple regions, so I'll scope what's feasible in the E2E test environment.

---

## Comment 20: Remove unused AWS_REGION replacement value

**File:** `test/e2e/replacement_values.py` (line 20)

**Original comment:**
> I don't see this replacement value actually used in the test resources. Is it still needed?

**Verdict:** ✅ Agree — dead code

The `AWS_REGION` replacement value isn't used in any test resource YAML templates. Remove it to keep things clean.

**Suggested response:**
> You're right, it's unused. I'll remove it.

---

## Comment 21: Property test for error classification may not be needed

**File:** `test/property/cluster_error_classification_test.go`

**Original comment:**
> I'm not sure that this test is actually needed.

**Verdict:** ⚠️ Partially agree — worth discussing

The reviewer questions whether this property test is needed. Given that terminal codes are handled declaratively in generator.yaml and the code generator produces the error classification logic, a separate property test may be redundant. However, if we want to verify the generated code handles edge cases correctly, it could have value. Lean toward removing it since the code generator is well-tested upstream.

**Suggested response:**
> Fair point. Since terminal code handling is generated by the code generator and tested upstream, this property test is likely redundant. I'll remove it unless there's DSQL-specific error classification logic that warrants dedicated testing.

---

## Comment 22: Property test for status conditions may not be needed

**File:** `test/property/cluster_status_condition_test.go`

**Original comment:**
> Not sure this is needed either.

**Verdict:** ⚠️ Partially agree — same reasoning as Comment 21

Same logic as Comment 21. If we move to `synced:when` config, the status condition logic is generated and tested upstream.

**Suggested response:**
> Agreed, especially if we adopt `synced:when`. The generated code will handle status conditions and is tested in the code-generator repo. I'll remove this.

---

## Comment 23: Why is Policy a custom field instead of using CreateClusterInput.Policy?

**File:** `generator.yaml` (Policy field config, lines 29-32)

**Original comment:**
> Q: Why is a customer field needed instead of using the Policy field from `CreateClusterInput`?

**Verdict:** ✅ Agree — this should be reconsidered

The reviewer asks why Policy is defined as a custom field with `type: string` and `compare.is_ignored: true` instead of using the native `CreateClusterInput.Policy` field. Currently `CreateClusterInput.Policy` is in the ignore list. The better approach is to:
1. Remove `CreateClusterInput.Policy` from `ignore.field_paths`
2. Remove the custom `Policy` field definition
3. Let the code generator infer the Policy field from the API model
4. Add `is_iam_policy: true` for proper comparison
5. Handle the separate Get/Put/Delete policy APIs via hooks in sdkUpdate

**Suggested response:**
> The custom field was used because Policy has separate CRUD APIs (GetClusterPolicy, PutClusterPolicy, DeleteClusterPolicy) and I wanted to handle it outside the normal delta path. But you're right — the cleaner approach is to use the native field from CreateClusterInput, remove it from the ignore list, add `is_iam_policy: true`, and handle the separate APIs in update hooks. I'll refactor this.

---

## Summary

All 22 comments from knottnt are valid and well-informed. The major architectural changes needed:

1. **Move policy management from sdkFind to sdkUpdate** (Comments 6, 7, 8, 9, 23) — biggest refactor
2. **Use `synced:when` config** instead of manual hooks (Comments 4, 5, 10)
3. **Fix terminal_codes** — remove AccessDeniedException and ServiceQuotaExceededException (Comment 1)
4. **Remove duplicate ARN field** (Comment 3)
5. **Improve E2E tests** — use acktest library functions, add multi-region coverage, remove dead code (Comments 13-22)


---

# New Comments from michaelhtm (2026-04-03)

Reviewer: **michaelhtm** (ACK org member, 5 inline comments)

---

## Comment 24: Remove deletionProtectionEnabled field for now

**File:** `apis/v1alpha1/cluster.go` (line 28, ClusterSpec.DeletionProtectionEnabled)

**Original comment:**
> Can we remove this field for now? We've had some issues when we supported it in the past (with rds controller). Maybe we can see if there is a need for it before we add it.

**Verdict:** ⚠️ Needs discussion — valid concern but may impact functionality

The reviewer has experience with deletion protection causing issues in the RDS controller. This is a cautious approach — ship without it and add later if needed. However, DSQL's deletion protection is a core safety feature. Removing it means users can't prevent accidental cluster deletion via the CR spec. Worth discussing whether the RDS issues apply here.

**Suggested response:**
> That's a fair concern given the RDS experience. What specific issues did you encounter? DSQL's deletionProtectionEnabled is a simple boolean on Create/Update — if the RDS issues were around reconciliation loops or state drift, we could mitigate those. Happy to defer if you think it's safer to add later.

---

## Comment 25: Identifier field config may be unnecessary

**File:** `apis/v1alpha1/generator.yaml` (line 36, fields.Identifier)

**Original comment:**
> nit: this may not be necessary to define here since code-generator will infer it is read_only and the primary key

**Verdict:** ✅ Agree — the code-generator can infer this

If the code-generator already infers `Identifier` as read-only and primary key from the API model (it's returned by GetCluster but not in CreateClusterInput), the explicit config is redundant. Removing it simplifies generator.yaml.

**Suggested response:**
> Good point, I'll remove the explicit Identifier field config and let the code-generator infer it.

---

## Comment 26: Read-only status fields don't need explicit from config

**File:** `apis/v1alpha1/generator.yaml` (line 58, fields for Endpoint/EncryptionDetails/CreationTime/Status)

**Original comment:**
> nit: These fields are already returned by CreateClusterOutput. They don't need to be defined here

**Verdict:** ✅ Agree — these are auto-inferred from the API model

Fields like Endpoint, EncryptionDetails, CreationTime, and Status are present in both CreateClusterOutput and GetClusterOutput. The code-generator can infer they're read-only status fields without explicit `from` config. Removing them cleans up generator.yaml significantly.

**Suggested response:**
> You're right — these are all in CreateClusterOutput so the code-generator handles them automatically. I'll remove the explicit field definitions.

---

## Comment 27: Question about Tags set/ignore config

**File:** `apis/v1alpha1/generator.yaml` (line 67, Tags field)

**Original comment:**
> is this necessary

**Verdict:** ⚠️ Likely unnecessary — the DSQL UpdateCluster API doesn't accept Tags, but the code-generator may already handle this

Looking at the DSQL UpdateCluster API, its request body only accepts `clientToken`, `deletionProtectionEnabled`, `kmsEncryptionKey`, and `multiRegionProperties`. Tags is NOT a parameter on UpdateCluster. The code-generator should already know this from the API model — if Tags isn't in the UpdateClusterInput shape, the generated sdkUpdate code won't try to set it.

The `set[method: Update, ignore: true]` config is typically needed when Tags IS present in the Update input shape but you want to handle it separately (via TagResource/UntagResource). Since DSQL's UpdateCluster doesn't include Tags at all, this config is likely redundant.

Other ACK controllers like autoscaling-controller and backup-controller don't use this pattern for Tags either — they handle tags through separate APIs without needing the `set.ignore` config.

However, we should verify by checking what the code-generator actually produces without this config. If removing it causes Tags to appear in the UpdateCluster payload (which would be a code-generator bug), we'd need to keep it.

**Suggested response:**
> Good question. Looking at the DSQL UpdateCluster API, Tags isn't in the UpdateClusterInput shape, so the code-generator shouldn't include it in the update payload regardless. I'll remove this config and verify the generated code still works correctly. If the code-generator incorrectly includes Tags without this hint, we can add it back.

---

## Comment 28: Add comment explaining why BypassPolicyLockoutSafetyCheck is ignored

**File:** `generator.yaml` (line 6, ignore.field_paths)

**Original comment:**
> nit: can we add a comment why this is ignored?

**Verdict:** ✅ Agree — good documentation practice

Adding a YAML comment explaining why `BypassPolicyLockoutSafetyCheck` is in the ignore list helps future maintainers. It's a safety mechanism that shouldn't be exposed to end users through the CRD.

**Suggested response:**
> Will add a comment. The field is a safety bypass that shouldn't be exposed to users — it allows skipping IAM policy lockout checks, which could lock the cluster owner out of their own cluster.
