"""File operations run by the summary task's ordinary gwf targets."""

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def rows(path):
    with open(path, newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["product", "amount"]:
            raise ValueError(f"{path} must have product,amount columns")
        yield from reader


def write(path, rows_to_write):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("product", "amount"))
        writer.writerows(rows_to_write)


def clean(source, destination):
    cleaned = []
    for row in rows(source):
        product = row["product"].strip()
        amount = int(row["amount"])
        if not product or amount < 0:
            raise ValueError(f"Invalid transaction in {source}: {row!r}")
        cleaned.append((product, amount))
    write(destination, cleaned)


def aggregate(source, destination):
    totals = defaultdict(int)
    for row in rows(source):
        totals[row["product"]] += int(row["amount"])
    write(destination, sorted(totals.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("clean", "aggregate"))
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    {"clean": clean, "aggregate": aggregate}[args.operation](
        args.source, args.destination
    )


if __name__ == "__main__":
    main()
