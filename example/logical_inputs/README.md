# Switch a document from text files to an archive

The `document` Task joins a heading and a body into a report. Its
`materialize` target reads a JSON descriptor that gwflow supplies in the
container. The descriptor identifies the format and gives the path to each
named file. Both source files have the basename `text.txt`.

The decoder writes `heading.txt` and `body.txt` for either format.
The `join` target consumes those outputs and writes the retained report.
The workflow owns the ZIP member names and reads only those two members.
See the [workflow](workflow.py) and [decoder](materialize.py).
The [alignment example](../README.md#archive-one-sample-with-spring) applies the same pattern to paired FASTQs and a SPRING archive.

## Run the two-file representation

Use an installed gwflow environment on Linux with Apptainer.
From the checkout root, make a working copy and build the example image.
If you already have the basic [test fixture](../../docs/validation/README.md#container-setup),
link its absolute SIF path as `python.sif` in the copy and skip the build.

```sh
demo=$(mktemp -d)
cp -R example/logical_inputs/. "$demo/"
apptainer build "$demo/python.sif" tests/fixtures/container.def
cd "$demo"
gwflow config set backend local
pwd
```

In a second terminal, activate the same environment and enter the printed
directory. Start the local scheduler there and leave it running.

```sh
gwflow workers -n 2
```

In the first terminal, run the Task and read its report. Record the attempt
ID from the status details.

```sh
gwflow run document
gwflow wait document --interval 1
gwflow status document --details --static
cat results/document/report.txt
```

The report contains these two lines.

```text
Meeting notes
The meeting starts at noon.
```

## Reuse the report, then force a new attempt

Clean the intermediates in the working copy. Pack the same heading and body
into an archive. Select it in `representation.txt` and remove the source files.

```sh
gwflow clean document --delete
python -m zipfile -c document.zip parts
printf 'zip\n' > representation.txt
rm parts/heading/text.txt parts/body/text.txt
gwflow run document --dry-run
gwflow run document
gwflow status document --details --static
```

The preview shows Reuse. The run submits zero jobs and keeps the same attempt
ID. The identity in the workflow asserts that both selections decode to the
same document. A change to its content requires a new identity. The decoder
script also counts as an ordinary tracked input.

Force fresh computation to read the ZIP archive. The log reports
`Materialized zip`, the attempt ID changes, and the report has the same text.

```sh
gwflow run document --force
gwflow wait document --interval 1
gwflow logs document__materialize --no-pager
gwflow status document --details --static
cat results/document/report.txt
```

Stop the scheduler in the second terminal with Ctrl-C.
