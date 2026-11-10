#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ljc
set -eu
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/rtl8822bu_setup.py" doctor "$@"
