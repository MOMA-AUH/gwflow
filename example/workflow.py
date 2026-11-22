"""Two multi-step alignment Tasks feed one report Task through retained outputs."""

import json
from pathlib import Path

from gwflow import Workflow
from task_library import align_reads, alignment_report


gwf = Workflow()
manifest = Path(__file__).parent / "data" / "reads.json"
samples = json.loads(manifest.read_text())
alignments = {
    sample: gwf.task(
        align_reads("data/reference.fa", sample, reads),
        alias="align", key=sample, result_dir=sample,
    )
    for sample, reads in samples.items()
}
gwf.task(
    alignment_report(
        {sample: alignment.outputs["statistics"] for sample, alignment in alignments.items()},
        "multiqc_config.yaml",
    ),
    alias="report", result_dir="report",
)
