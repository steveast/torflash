#!/bin/sh
cd "$(dirname "$0")"
exec /usr/bin/python3 rutor_search.py "$@"
