#!/usr/bin/env bash

npm_config_before="$(node -p 'new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString()')"
export npm_config_before
