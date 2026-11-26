#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

work=$(mktemp -d)
container=
cleanup() {
    if [[ -n "$container" ]]; then
        docker rm "$container" >/dev/null
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT

if [[ -n "${MEAL_DISPATCH_KAFKA_LIBS:-}" ]]; then
    libs=$MEAL_DISPATCH_KAFKA_LIBS
else
    container=$(docker create apache/kafka:3.7.2)
    mkdir "$work/libs"
    docker cp "$container:/opt/kafka/libs/." "$work/libs"
    libs=$work/libs
fi

jdk_bin=${MEAL_DISPATCH_JDK:+$MEAL_DISPATCH_JDK/bin/}
mkdir "$work/classes"
mkdir -p dist
"${jdk_bin}javac" -J-XX:ActiveProcessorCount=2 --release 11 -encoding UTF-8 \
    -classpath "$libs/*" -d "$work/classes" src/*.java
"${jdk_bin}jar" --create --file "$work/meal-dispatch.jar" -C "$work/classes" .
mv "$work/meal-dispatch.jar" dist/
sha256sum dist/meal-dispatch.jar
