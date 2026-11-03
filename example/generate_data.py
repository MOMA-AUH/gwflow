"""Regenerate the committed artificial reference and paired reads using only Python."""

import argparse
import json
from pathlib import Path
import random


SEED = 20261103
REFERENCE_LENGTH = 10_000
READ_LENGTH = 150
FRAGMENT_LENGTH = 350
PAIRS_PER_SAMPLE = 100
UNMAPPED_PAIRS = {"sample_a": 0, "sample_b": 20}
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def reverse_complement(sequence):
    return sequence.translate(COMPLEMENT)[::-1]


def kmers(sequence, length):
    return {sequence[start:start + length]
            for start in range(len(sequence) - length + 1)}


def generate(output_dir):
    rng = random.Random(SEED)

    def dna(length):
        return "".join(rng.choice("ACGT") for _ in range(length))

    reference = dna(REFERENCE_LENGTH)
    # Avoid repeated seeds in the artificial reference. Check both strands.
    seeds = kmers(reference, 21) | kmers(reverse_complement(reference), 21)
    assert len(seeds) == 2 * (REFERENCE_LENGTH - 21 + 1)
    reference_seeds = kmers(reference, 15) | kmers(reverse_complement(reference), 15)

    output_dir.mkdir(parents=True, exist_ok=True)
    fasta = ">synthetic_reference\n" + "".join(
        reference[start:start + 80] + "\n"
        for start in range(0, REFERENCE_LENGTH, 80)
    )
    (output_dir / "reference.fa").write_text(fasta, encoding="ascii")

    expected = {
        "description": "Wholly artificial DNA; no biological source data or sequencing errors.",
        "generator": "example/generate_data.py",
        "seed": SEED,
        "reference": {"name": "synthetic_reference", "length": REFERENCE_LENGTH},
        "read_length": READ_LENGTH,
        "fragment_length": FRAGMENT_LENGTH,
        "quality": {"encoding": "Phred+33", "score": 40},
        "truth_coordinates": "truth.tsv uses 1-based inclusive fragment coordinates; * means unmapped.",
        "samples": {},
    }
    truth = ["sample\tread_pair\treference\tfragment_start\tfragment_end\n"]
    for sample, unmapped_pairs in UNMAPPED_PAIRS.items():
        reads = [[], []]
        # Fragments stay well away from either reference edge.
        starts = rng.sample(range(500, REFERENCE_LENGTH - FRAGMENT_LENGTH - 500),
                            PAIRS_PER_SAMPLE)
        for number, start in enumerate(starts, 1):
            pair = f"{sample}_{number:04d}"
            if number <= PAIRS_PER_SAMPLE - unmapped_pairs:
                fragment = reference[start:start + FRAGMENT_LENGTH]
                truth.append(f"{sample}\t{pair}\tsynthetic_reference\t"
                             f"{start + 1}\t{start + FRAGMENT_LENGTH}\n")
            else:
                # No shared 15-mer on either strand: even shorter seeds than
                # minimap2's short-read preset cannot match the reference.
                fragment = dna(FRAGMENT_LENGTH)
                while kmers(fragment, 15) & reference_seeds:
                    fragment = dna(FRAGMENT_LENGTH)
                truth.append(f"{sample}\t{pair}\t*\t*\t*\n")
            sequences = (fragment[:READ_LENGTH],
                         reverse_complement(fragment[-READ_LENGTH:]))
            for mate, sequence in enumerate(sequences):
                reads[mate].append(f"@{pair}/{mate + 1}\n{sequence}\n+\n"
                                   f"{'I' * READ_LENGTH}\n")
        for mate, records in enumerate(reads, 1):
            (output_dir / f"{sample}_R{mate}.fastq").write_text(
                "".join(records), encoding="ascii")
        mapped_reads = 2 * (PAIRS_PER_SAMPLE - unmapped_pairs)
        expected["samples"][sample] = {
            "read_pairs": PAIRS_PER_SAMPLE,
            "primary_reads": 2 * PAIRS_PER_SAMPLE,
            "mapped_primary_reads": mapped_reads,
            "unmapped_primary_reads": 2 * unmapped_pairs,
            "properly_paired_reads": mapped_reads,
            "mapped_percent": 100 * mapped_reads / (2 * PAIRS_PER_SAMPLE),
        }

    (output_dir / "truth.tsv").write_text("".join(truth), encoding="ascii")
    (output_dir / "expected.json").write_text(
        json.dumps(expected, indent=2) + "\n", encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("data"),
                        help="destination directory (default: data beside this script)")
    generate(parser.parse_args().output_dir)
