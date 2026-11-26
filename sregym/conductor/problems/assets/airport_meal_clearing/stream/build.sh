#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

cache="${MEAL_BUILD_CACHE:-$PWD/.build}"
lab="${MEAL_LAB_CACHE:-}"
dist="${MEAL_BUILD_DIST:-$PWD/dist}"
mkdir -p "$cache" "$dist"
work="$(mktemp -d "$cache/build.XXXXXX")"
trap 'rm -rf "$work"' EXIT

fetch() {
    local file="$1" checksum="$2" url="$3" lab_name="$4" tool=sha256sum
    if [[ ${#checksum} == 128 ]]; then tool=sha512sum; fi
    if [[ ! -f "$cache/$file" ]]; then
        if [[ -n "$lab" && -f "$lab/$lab_name" ]]; then
            cp "$lab/$lab_name" "$work/$file"
        else
            curl --fail --silent --show-error --location --retry 3 "$url" -o "$work/$file"
        fi
        printf '%s  %s\n' "$checksum" "$work/$file" | "$tool" --check --status
        mv "$work/$file" "$cache/$file"
    fi
    printf '%s  %s\n' "$checksum" "$cache/$file" | "$tool" --check
}

jdk=amazon-corretto-11.0.33.1.1-linux-x64.tar.gz
fetch "$jdk" 4b97a5dbc3fb431bf6fed5f06028bcd5830d9cfc1f30655d87abd9ed74fc301c \
    "https://corretto.aws/downloads/resources/11.0.33.1.1/$jdk" jdk11.tgz
mkdir -p "$work/jdk"
tar -xf "$cache/$jdk" -C "$work/jdk" --strip-components=1
javac="$work/jdk/bin/javac"
export JAVA_TOOL_OPTIONS=-XX:ActiveProcessorCount=2

flink_job() {
    local flink="$1" flink_sha="$2" connector="$3" connector_sha="$4" lab_jar="$5"
    local binary="flink-$flink-bin-scala_2.12.tgz"
    local sql_jar="flink-sql-connector-kafka-$connector.jar"
    fetch "$binary" "$flink_sha" "https://archive.apache.org/dist/flink/flink-$flink/$binary" "flink-$flink.tgz"
    fetch "$sql_jar" "$connector_sha" \
        "https://repo.maven.apache.org/maven2/org/apache/flink/flink-sql-connector-kafka/$connector/$sql_jar" "$lab_jar"
    mkdir -p "$work/flink" "$work/classes"
    tar -xf "$cache/$binary" -C "$work/flink" --strip-components=1 --wildcards "*/lib/*"
    "$javac" --release 11 -encoding UTF-8 -classpath "$work/flink/lib/*:$cache/$sql_jar" \
        -d "$work/classes" src/MealOrders.java
    python3 pack_jar.py "$work/classes" "$work/meal-orders-$flink.jar" "$cache/$sql_jar"
    rm -rf "$work/flink" "$work/classes"
    mv "$work/meal-orders-$flink.jar" "$dist/"
}

flink_job 1.18.1 d284ced497c5c09dae02d87552e73c8296b8522149d4cf4c4f53c4b4e3dcec489568a6f713c0dce0813cf0db6ed45e0a2651a997d755b88a05f6f0bccd9ff520 \
    3.1.0-1.18 ad001ada5a43aca44341790a1e28135c808034be2e6b6b47e0394ad8d4d2bf0b flink-kafka.jar
flink_job 1.19.0 56fa2e51d5feb01b973d651d37576c7b65a46c2b1f3158bbe7375b0aaeb52a00dc1cf58900ebbc0d7e4e8252a0c0baf5792b96c3f55b062a30e2acba14b0eebf \
    3.2.0-1.19 c3eda3cd295c1cdfb1dcbae4e0fd318d82e2dd6f073328e30cd79f1d9a990775 flink-kafka-119.jar

kafka=kafka_2.13-3.7.2.tgz
fetch "$kafka" 65c09fbe8c78098b1efb26632a35d90ee709327a7aab13b37b2023692e1649105714ea513253a19f2cb685dc2c0c3837f32e39e9fb1d8d2367fe4650e5ad3cdc \
    "https://archive.apache.org/dist/kafka/3.7.2/$kafka" kafka-3.7.2.tgz
mkdir -p "$work/kafka" "$work/classes"
tar -xf "$cache/$kafka" -C "$work/kafka" --strip-components=1 --wildcards "*/libs/*"
"$javac" --release 11 -encoding UTF-8 -classpath "$work/kafka/libs/*" \
    -d "$work/classes" src/KafkaOps.java
python3 pack_jar.py "$work/classes" "$work/kafka-ops.jar"
mv "$work/kafka-ops.jar" "$dist/"
(cd "$dist" && sha256sum ./*.jar)
