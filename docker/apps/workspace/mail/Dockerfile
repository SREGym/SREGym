# Tenant mail: Postfix for transport, Dovecot for mailboxes (IMAP + LMTP).
# One image, two roles; the role and the site settings come from the
# environment so the configuration stays visible in the pod spec and in the
# files the entrypoint writes.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN set -eu; apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        postfix=3.7.11-0+deb12u1 \
        dovecot-imapd=1:2.3.19.1+dfsg1-2.1+deb12u7 dovecot-lmtpd=1:2.3.19.1+dfsg1-2.1+deb12u7 \
        ca-certificates netcat-openbsd procps; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd -g 5000 vmail; useradd -u 5000 -g vmail -d /var/mail -s /usr/sbin/nologin vmail
COPY dovecot.conf /etc/dovecot/dovecot.conf
COPY entrypoint.sh /usr/local/bin/mail-entrypoint
ENTRYPOINT ["/usr/local/bin/mail-entrypoint"]
