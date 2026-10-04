# Share acquired images across workflows

Registry-image acquisition will use one image cache per user shared across that user's workflows, avoiding duplicate acquired images without introducing a cache writable by multiple users. The default location will be `~/.cache/gwflow/images`, with one environment-variable override to select storage visible at the same path from the frontend and compute nodes. This accepts coordination between workflows in exchange for shared storage and acquisition; it rules out a separate cache owned by each workflow.

gwflow never deletes cached images, including during work cleanup, and provides no eviction policy or image-cache cleanup command. Users control removal and, as with other external inputs, keep cached images available and stable while jobs use them; gwflow does not coordinate deletion across workflows.
