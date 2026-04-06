# DSQL Controller: DeletionProtectionEnabled Handling

## Context

The DSQL `CreateCluster` API defaults `deletionProtectionEnabled` to `true`. This means every cluster created through ACK has deletion protection enabled unless explicitly overridden. The question is how the DSQL ACK controller should handle this field.

The RDS controller exposed `DeletionProtection` as a CRD spec field, which led to a known deadlock issue: [community#2436](https://github.com/aws-controllers-k8s/community/issues/2436).

## The RDS Deadlock Problem

When `DeletionProtection` is a CRD spec field and set to `true`:

1. User (or GitOps tool like FluxCD/ArgoCD) deletes the CR
2. Kubernetes sets `deletionTimestamp` on the resource
3. Controller calls `DeleteCluster` → AWS rejects: "Cannot delete protected cluster"
4. Controller sets `ACK.Terminal: true` → stops reconciling
5. Kubernetes blocks spec updates once `deletionTimestamp` is set → user cannot patch `deletionProtection: false`
6. **Deadlock** — resource is stuck forever, requires manual AWS console intervention

## Options Considered

### Option A: Keep field in CRD, no special handling (RDS approach)

- `DeletionProtectionEnabled` is a regular spec field
- Users control it directly
- Deadlock occurs if user deletes CR while protection is `true`
- Workaround: user must disable via AWS console, then patch the CR

**Pros:** Full user control, matches AWS API behavior
**Cons:** Known deadlock (community#2436), poor GitOps experience

### Option B: Remove field from CRD, disable protection on create

- `DeletionProtectionEnabled` excluded via `ignore.field_paths`
- Create hook sets `DeletionProtectionEnabled=false` explicitly
- Clusters are never protected from out-of-band deletion
- Users rely on ACK `deletion-policy: retain` for protection

**Pros:** No deadlock, simple
**Cons:** Clusters unprotected from console/CLI deletion, changes DSQL default behavior

### Option C: Remove field from CRD, disable protection only on delete

- `DeletionProtectionEnabled` excluded via `ignore.field_paths`
- No create hook — DSQL defaults to `true` (cluster is protected)
- Delete hook calls `UpdateCluster(deletionProtectionEnabled=false)` before `DeleteCluster`
- Clusters are protected from out-of-band deletion during their lifetime

**Pros:** Clusters protected from console/CLI deletion, no deadlock, clean ACK deletion
**Cons:** Controller silently changes an AWS setting the user didn't ask to change

### Option D (Recommended): Remove field from CRD, enable protection on create, disable on delete

- `DeletionProtectionEnabled` excluded via `ignore.field_paths`
- Create hook explicitly sets `DeletionProtectionEnabled=true`
- Delete hook calls `UpdateCluster(deletionProtectionEnabled=false)` before `DeleteCluster`
- Users use ACK `deletion-policy: retain` annotation to prevent deletion

**Pros:**
- Clusters are protected from out-of-band deletion (console, CLI, other tools)
- ACK can always clean up — no deadlock
- Single deletion protection mechanism for users (`deletion-policy: retain`)
- No confusing dual-mechanism (AWS deletion protection vs ACK deletion policy)
- Consistent with ACK behavior — deleting a CR deletes the AWS resource

**Cons:**
- Users cannot set `deletionProtectionEnabled=false` if they want to allow console deletion
- Controller manages the field opaquely

### Option E: Keep field in CRD, add validating webhook to reject delete

- `DeletionProtectionEnabled` is a regular spec field
- Validating webhook rejects `kubectl delete` when field is `true`
- `deletionTimestamp` never gets set → no deadlock
- User sets field to `false` first, then deletes

**Pros:** Full user control, no deadlock, clean UX
**Cons:** Requires webhook infrastructure, more complex to maintain, not standard ACK pattern

## Recommendation

**Option D** — Remove the field from the CRD, explicitly enable protection on create, and disable it in the delete hook.

Rationale:
- ACK owns the resource lifecycle. When a user deletes a CR, they expect the AWS resource to be deleted. This is the contract.
- Out-of-band deletion protection (console/CLI) is valuable and comes for free with DSQL's default.
- The `deletion-policy: retain` annotation is the ACK-native way to prevent deletion. Having two mechanisms (AWS deletion protection + ACK deletion policy) is confusing.
- This avoids the RDS deadlock entirely without requiring runtime changes.

## Implementation (Option D)

1. `generator.yaml`: Keep `CreateClusterInput.DeletionProtectionEnabled` and `UpdateClusterInput.DeletionProtectionEnabled` in `ignore.field_paths`
2. `sdk_create_post_build_request.go.tpl`: Set `input.DeletionProtectionEnabled = aws.Bool(true)`
3. `sdk_delete_pre_build_request.go.tpl`: Call `UpdateCluster` with `DeletionProtectionEnabled=false`, then proceed with `DeleteCluster`
4. Delete hook comment references the deadlock issue and points users to `deletion-policy: retain`

## Open Questions

1. ~~Should we document this behavior in the CRD description or controller README?~~ **Yes** — document in the controller README that ACK manages `deletionProtectionEnabled` automatically and that users should use `deletion-policy: retain` for deletion protection.
2. Should the delete hook log a message when disabling deletion protection?
3. Are there cases where a user would want `deletionProtectionEnabled=false` on the AWS resource while managed by ACK?
