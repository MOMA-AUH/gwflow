"""Multi-step alignment Tasks and a report built from their retained results."""

from gwflow import Task, shell, task_template


# Public Python image, pinned to the same digest as the container test fixture.
PYTHON = "docker://python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
# Bioconda builds on Quay.io, pinned to their Linux x86_64 manifests.
# spring:1.1.1--h4ac6f70_3
SPRING = "docker://quay.io/biocontainers/spring@sha256:70d8c77b81b2015a0912570470531e4f92aeaedf764f63686e06949108b9d028"
# minimap2:2.28--h577a1d6_4
MINIMAP2 = "docker://quay.io/biocontainers/minimap2@sha256:8310d4996f929c1dbde6d2b17b755d211d6e4493ce6cc3d514842cf8df27af80"
# samtools:1.21--h96c455f_1
SAMTOOLS = "docker://quay.io/biocontainers/samtools@sha256:d1585753f42f3b89a3c64bcb4bf8f5553befd15c1b388d954a8b995e21ecf9a1"
# multiqc:1.25.2--pyhdfd78af_0
MULTIQC = "docker://quay.io/biocontainers/multiqc@sha256:d939a5fc6c5b505f7bb148ce3527fb9850c6e8f3f4eab7cc5a4be180f0533f46"


@task_template
def align_reads(reference, sample, reads):
    """Materialize the paired reads and keep their alignment, index, and statistics."""
    decoder = "materialize_reads.py"
    task = Task(inputs=[reference, decoder])
    if reads["format"] == "fastq":
        files = {mate: f"data/{sample}_{mate}.fastq" for mate in ("R1", "R2")}
    elif reads["format"] == "spring":
        files = {"archive": f"data/{sample}.spring"}
    else:
        raise ValueError(f"Unknown read format for {sample}: {reads['format']!r}")
    source = task.logical_input("reads", identity=reads["sha256"], files=files,
                                metadata={"format": reads["format"]})
    prepare = task.target("prepare_reads", inputs=[decoder, source], outputs=["reads.tar"],
                          image=PYTHON, cores=1, memory="1g", walltime="00:05:00")
    prepare << shell("python {decoder} prepare {descriptor}", decoder=decoder, descriptor=source)

    materialize = task.target("materialize", inputs=[prepare.output("reads.tar")],
                              outputs=["R1.fastq", "R2.fastq"],
                              image=SPRING, cores=1, memory="1g", walltime="00:05:00")
    materialize << shell(
        """tar -xf {bundle}
if [ -f input.spring ]; then
    spring -d -t 1 -i input.spring -o R1.fastq R2.fastq
fi""",
        bundle=prepare.output("reads.tar"),
    )
    read1, read2 = materialize.output("R1.fastq"), materialize.output("R2.fastq")

    verify = task.target("verify_reads", inputs=[decoder, source, read1, read2], outputs=["verified.txt"],
                         image=PYTHON, cores=1, memory="1g", walltime="00:05:00")
    verify << shell("python {decoder} verify {descriptor} {read1} {read2}",
                    decoder=decoder, descriptor=source, read1=read1, read2=read2)

    align = task.target(
        "align", inputs=[reference, read1, read2, verify.output("verified.txt")], outputs=["alignment.sam"],
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
