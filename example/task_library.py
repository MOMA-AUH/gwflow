"""Multi-step alignment Tasks and a report built from their retained results."""

from gwflow import Task, shell, task_template


# Bioconda builds on Quay.io, pinned to their Linux x86_64 manifests.
# minimap2:2.28--h577a1d6_4
MINIMAP2 = "docker://quay.io/biocontainers/minimap2@sha256:8310d4996f929c1dbde6d2b17b755d211d6e4493ce6cc3d514842cf8df27af80"
# samtools:1.21--h96c455f_1
SAMTOOLS = "docker://quay.io/biocontainers/samtools@sha256:d1585753f42f3b89a3c64bcb4bf8f5553befd15c1b388d954a8b995e21ecf9a1"
# multiqc:1.25.2--pyhdfd78af_0
MULTIQC = "docker://quay.io/biocontainers/multiqc@sha256:d939a5fc6c5b505f7bb148ce3527fb9850c6e8f3f4eab7cc5a4be180f0533f46"


@task_template
def align_reads(reference, read1, read2):
    """Keep a sorted alignment, its index, and statistics from four targets."""
    task = Task(inputs=[reference, read1, read2])

    align = task.target(
        "align", inputs=[reference, read1, read2], outputs=["alignment.sam"],
        image=MINIMAP2, cores=2, memory="1g", walltime="00:05:00",
    )
    align << shell(
        "minimap2 -ax sr -t 2 {reference} {read1} {read2} > {sam}",
        reference=reference, read1=read1, read2=read2, sam=align.output("alignment.sam"),
    )

    sort = task.target(
        "sort", inputs=[align.output("alignment.sam")], outputs=["alignment.bam"],
        image=SAMTOOLS, cores=1, memory="1g", walltime="00:05:00",
    )
    sort << shell(
        "samtools sort -m 128M -o {bam} {sam}",
        bam=sort.output("alignment.bam"), sam=align.output("alignment.sam"),
    )

    index = task.target(
        "index", inputs=[sort.output("alignment.bam")], outputs=["alignment.bam.bai"],
        image=SAMTOOLS, cores=1, memory="1g", walltime="00:05:00",
    )
    index << shell(
        "samtools index {bam} {bai}",
        bam=sort.output("alignment.bam"), bai=index.output("alignment.bam.bai"),
    )

    statistics = task.target(
        "statistics", inputs=[sort.output("alignment.bam")], outputs=["flagstat.txt"],
        image=SAMTOOLS, cores=1, memory="1g", walltime="00:05:00",
    )
    statistics << shell(
        "samtools flagstat {bam} > {statistics}",
        bam=sort.output("alignment.bam"), statistics=statistics.output("flagstat.txt"),
    )

    # The SAM stays inside this Task. Only retained references can cross Tasks.
    task.retain("bam", source=sort.output("alignment.bam"), path="alignment.bam")
    task.retain("index", source=index.output("alignment.bam.bai"), path="alignment.bam.bai")
    task.retain("statistics", source=statistics.output("flagstat.txt"), path="flagstat.txt")
    return task


@task_template
def alignment_report(statistics, config):
    """Combine named samples' retained statistics, even after their work is cleaned."""
    inputs = [*statistics.values(), config]
    task = Task(inputs=inputs)
    report = task.target(
        "report", inputs=inputs,
        outputs=["multiqc_report.html", "multiqc_data/multiqc_general_stats.txt"],
        image=MULTIQC,
        # Both producers retain flagstat.txt. Give each staged input its sample name.
        stage_as={f"{sample}.txt": source for sample, source in statistics.items()},
        cores=1, memory="2g", walltime="00:05:00",
    )
    report << shell(
        "multiqc --no-version-check --module samtools --require-logs --config {config} --outdir . .",
        config=config,
    )
    task.retain("html", source=report.output("multiqc_report.html"), path="report.html")
    task.retain("summary", source=report.output("multiqc_data/multiqc_general_stats.txt"), path="summary.tsv")
    return task
