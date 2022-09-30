#!/bin/bash

set -eu
set -o pipefail

cd "$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd ..

. scripts/vars.sh

python3 queries.py --scale_factor ${SF} --data_dir ${UMBRA_CSV_DIR} $@
