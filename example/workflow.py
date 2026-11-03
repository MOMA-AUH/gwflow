"""Two multi-step alignment Tasks feed one report Task through retained outputs."""

from gwflow import Workflow
from task_library import align_reads, alignment_report


gwf = Workflow()
samples = ("sample_a", "sample_b")
alignments = {
    sample: gwf.task(
        align_reads("data/reference.fa", f"data/{sample}_R1.fastq", f"data/{sample}_R2.fastq"),
        alias="align", key=sample, result_dir=sample,
    )
    for sample in samples
}
gwf.task(
    alignment_report(
        {sample: alignment.outputs["statistics"] for sample, alignment in alignments.items()},
        "multiqc_config.yaml",
    ),
    alias="report", result_dir="report",
)
