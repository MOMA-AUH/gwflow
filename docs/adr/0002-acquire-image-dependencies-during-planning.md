# Acquire image dependencies during workflow planning

Registry-image support will initially accept explicit `docker://` references to anonymously accessible OCI/Docker images, acquire them through Apptainer, and use the resulting local SIF images for execution. Image dependencies must be available for meaningful planning, so `gwf run`, `gwf status`, `gwf explain`, and `gwf run --dry-run` will acquire missing registry images, including those belonging to otherwise reusable Tasks. This deliberately extends the previous inspection contract to permit image acquisition while preserving the prohibition on inspection changing task completion records or results, so inspection and execution planning use the same available dependencies.

An acquisition failure blocks the affected Task and prevents all job submission for that workflow, following the existing planning rule. Inspection can report that blockage alongside the other Tasks.

Acquisition runs on the frontend; compute nodes execute the cached SIF using the same shared filesystem path. The initial deployment scope requires matching frontend and compute-node CPU architectures, avoiding an architecture-selection interface or worker-side downloads.
