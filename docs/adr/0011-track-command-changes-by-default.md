# Require command tracking in managed workflows

For v0.8.0, managed workflows always track command changes, replacing the
inherited opt-in behavior and removing the managed-workflow opt-out. A changed
computation command requires a fresh Task attempt under the existing lifecycle
checks, because silently reusing results after a command edit is unsuitable for
production workflows. This establishes one command-based Reuse rule, accepting
that harmless command-text changes may also require expensive
recomputation.

The inherited `use_spec_hashes=false` setting is rejected for managed workflows
with a clear error explaining that command tracking is mandatory and the setting
must be removed. Do not silently ignore it or retain a warning-only compatibility
path. This decision does not add package-version or environment-based
invalidation and does not change the separation of factory provenance from Reuse in
[ADR 0006](0006-keep-factory-provenance-separate-from-reuse.md).
