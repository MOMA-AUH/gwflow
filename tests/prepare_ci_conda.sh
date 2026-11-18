#!/usr/bin/env bash
# Construct new source/build installations, also used by the cache measurement workflow.
set -euo pipefail
started=$SECONDS
case "$1" in
  source)
    conda create -y -n gwflow-tests python=3.12 pip gwf=3.0.0 -c gwforg -c conda-forge
    conda run -n gwflow-tests python -m pip install .
    conda run -n gwflow-tests python -m pip install ./tests/fixtures/packaged_tasks/packages/summary-task ./tests/fixtures/packaged_tasks/packages/report-task
    conda run -n gwflow-tests python -m pip check
    ;;
  build)
    conda install -y -n base conda-build -c conda-forge
    conda build conda-recipe --no-anaconda-upload -c gwforg -c conda-forge --output-folder "$RUNNER_TEMP/conda-dist"
    version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
    artifact=$(find "$RUNNER_TEMP/conda-dist/noarch" -name "gwflow-${version}-*.conda" -print -quit)
    test -n "$artifact"
    conda create -y -p "$RUNNER_TEMP/gwflow-build" python=3.12 "gwflow=$version" -c "file://$RUNNER_TEMP/conda-dist" -c gwforg -c conda-forge
    conda run -p "$RUNNER_TEMP/gwflow-build" python -m pip install --no-deps ./tests/fixtures/packaged_tasks/packages/summary-task ./tests/fixtures/packaged_tasks/packages/report-task
    # This transfer capture is unqualified until every installed suite shard passes.
    python tests/conda_runtime.py capture --prefix "$RUNNER_TEMP/gwflow-build" --artifact "$artifact" --source "$PWD" --output "$RUNNER_TEMP/ci-runtime"
    ;;
  *) echo "Expected source or build profile" >&2; exit 1 ;;
esac
echo "Conda setup seconds: $((SECONDS - started))"
python tests/conda_downloads.py summary --packages "$CONDA_PKGS_DIRS"
