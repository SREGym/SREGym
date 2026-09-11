# Preserve Train Ticket's Nacos schema and initialization contract. The
# original client is MariaDB 10.5.12, whose official image supports ARM64.
FROM --platform=linux/amd64 codewisdom/mysqlclient@sha256:9201e8dfe5eb4e845259730a6046c7b905566119d760ed7d5aef535ace972216 AS original
FROM mariadb:10.5.12@sha256:f0e9a95a715f5f1233f0513b6ccc83e2d5692cd787bc9425e11cef667cf086ec
COPY --from=original /with-wait.sh /with-wait.sh
COPY --from=original /init/ /init/
ENTRYPOINT ["/with-wait.sh"]
CMD ["/init/init.sh"]
