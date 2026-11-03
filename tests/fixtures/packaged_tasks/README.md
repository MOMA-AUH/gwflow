# Packaged Task fixtures

These fixtures test independently installed Task packages, retained outputs,
and consumer execution after producer cleanup. They also support release smoke
checks and runtime qualification. Package names and versions form part of that
validation contract.

Install both packages from the repository root:

```sh
python -m pip install ./tests/fixtures/packaged_tasks/packages/summary-task ./tests/fixtures/packaged_tasks/packages/report-task
```

The container definitions exercise commands installed without gwflow inside the
image. Build them from the repository root. See
[the validation guide](../../../docs/validation/README.md) for the complete checks.
The public walkthrough is [the bioinformatics example](../../../example/README.md).
