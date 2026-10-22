#!/bin/sh
set -eu

data_dir=/var/lib/chat-archive
chown 10001:10001 "${data_dir}"
chmod 0700 "${data_dir}"
