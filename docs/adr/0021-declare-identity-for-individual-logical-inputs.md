# Declare identity for individual logical inputs

[ADR 0012](0012-use-explicit-force-for-untracked-changes.md) keeps explicit force
as the remedy for untracked computation changes. We revisited that decision for
externally supplied data with multiple physical representations. A workflow may
declare the logical identity of one input and take responsibility for decoding
its selected files. The declaration makes the author's equivalence claim
explicit while keeping whole-Task validity subject to the usual checks on
commands, ordinary inputs, images, producer attempts, and retained outputs.
Computation-affecting values belong in the logical identity or another tracked
declaration. Representation metadata describes how to decode that input.

Each attempt freezes its selection, accepts the physical Input baseline at
preparation, and supplies resolved file paths through an attempt-owned descriptor.
Retries use that frozen evidence so a later declaration cannot substitute files
beneath previously checked work. A change of identity always requires a fresh
attempt. A change of representation requires a fresh attempt while computation
or Completion is unfinished. The generated descriptor path belongs to execution
storage.

After successful Completion, Reuse accepts a different representation
with the same declared logical input name and identity. The workflow owns the
decoding and keeps its target declarations and command templates stable across
representations. Reuse preserves the completed attempt's selection, descriptor,
and physical Input baseline. Explicit force starts a new attempt that accepts
the replacement files at preparation. Reuse and Repair from valid checked
work can proceed while the physical files are unavailable. Ordinary inputs
and Image dependencies retain their availability requirements.

Initial result installation follows the final physical-baseline check. The
owned, installed directory matching the complete manifest provides durable
evidence that this check passed. An interrupted attempt can finish recording
Completion from that installed set. A staged manifest alone still requires
the frozen inputs before installation. A selected Repair retains evidence of
the checked work under its attempt and can resume with the same availability
rules as the original Repair.
