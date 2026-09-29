"""File operations run by the report task's ordinary gwf targets."""

import argparse
import csv
from pathlib import Path


def read(path, columns):
    with open(path, newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != columns:
            raise ValueError(f"{path} must have {','.join(columns)} columns")
        return list(reader)


def write(path, columns, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def join(sales_path, returns_path, destination):
    columns = ["product", "amount"]
    sales = {row["product"]: int(row["amount"]) for row in read(sales_path, columns)}
    returns = {
        row["product"]: int(row["amount"]) for row in read(returns_path, columns)
    }
    joined = (
        (product, sales.get(product, 0), returns.get(product, 0))
        for product in sorted(sales.keys() | returns.keys())
    )
    write(destination, ["product", "sales", "returns"], joined)


def finalize(source, destination):
    columns = ["product", "sales", "returns"]
    output = (
        (row["product"], row["sales"], row["returns"],
         int(row["sales"]) - int(row["returns"]))
        for row in read(source, columns)
    )
    write(destination, [*columns, "net"], output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    join_parser = subparsers.add_parser("join")
    join_parser.add_argument("sales")
    join_parser.add_argument("returns")
    join_parser.add_argument("destination")
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("source")
    finalize_parser.add_argument("destination")
    args = parser.parse_args()
    if args.operation == "join":
        join(args.sales, args.returns, args.destination)
    else:
        finalize(args.source, args.destination)


if __name__ == "__main__":
    main()
