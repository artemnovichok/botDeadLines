#!/bin/sh
set -eu
umask 077
if [ "$#" -eq 0 ]; then
    alembic upgrade head
    set -- python -m deadline_bot serve
fi
exec "$@"
