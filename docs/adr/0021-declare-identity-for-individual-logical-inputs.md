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
beneath previously checked work. In this first local execution contract, changing
either identity or representation requires a fresh attempt. The generated
descriptor path is execution storage and contributes nothing to Reuse identity.
