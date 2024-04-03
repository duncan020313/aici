#!/bin/sh

if [ "X$AICI_API_BASE" = "X" ] ; then
  export AICI_API_BASE="http://0.0.0.0:4242/v1/"
fi

PYTHONPATH=`dirname $0`/py \
python3 -m pyaici.cli "$@"
