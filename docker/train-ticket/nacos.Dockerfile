# The original Java application already bundles ARM64 RocksDB JNI.
# Keep its complete distribution and Java 8u292; replace only the JVM/OS.
FROM --platform=linux/amd64 nacos/nacos-server@sha256:365099d5519bc0f146f373bb77d2642944657d6d23254248a1dc150f255e314d AS original
FROM adoptopenjdk:8u292-b10-jdk-hotspot@sha256:b573593b66c9b8e3d53a3686561e4d2416ae999c9137323a4962cf8ff3fcce4d
ENV MODE=cluster PREFER_HOST_MODE=ip BASE_DIR=/home/nacos \
    CLASSPATH=.:/home/nacos/conf: CLUSTER_CONF=/home/nacos/conf/cluster.conf \
    FUNCTION_MODE=all NACOS_USER=nacos JAVA=/opt/java/openjdk/bin/java \
    JVM_XMS=1g JVM_XMX=1g JVM_XMN=512m JVM_MS=128m JVM_MMS=320m \
    NACOS_DEBUG=n TOMCAT_ACCESSLOG_ENABLED=false TIME_ZONE=Asia/Shanghai
COPY --from=original /home/nacos/ /home/nacos/
WORKDIR /home/nacos
EXPOSE 8848 9848 9849 7848
ENTRYPOINT ["bin/docker-startup.sh"]
