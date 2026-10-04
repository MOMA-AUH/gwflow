# Pull registry images only when their cached files are missing

Registry image references will reuse their cached SIF files while those files exist; a missing cached file is the only trigger for a new pull, with no explicit refresh option or periodic registry checks. This permits image dependency resolution from a warm cache without registry access and means a moved tag is adopted only when its cached file is missing. Digest-pinned references will also be supported for reproducible source selection.
