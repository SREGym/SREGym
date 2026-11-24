#!/bin/sh
# Start one mail role in the foreground.
#   mailbox    Dovecot. Accounts come from /etc/mail-users/users (user@domain:password).
#   transport  Postfix. Site settings come from /etc/postfix-site/site.cf, one
#              `postconf -e` assignment per line, applied on every start.
set -eu
role=${1:-}

case "$role" in
mailbox)
    # user:password lines become full passwd-file entries (uid, gid, home).
    awk -F: 'NF >= 2 { split($1, a, "@"); printf "%s:{PLAIN}%s:5000:5000::/var/mail/%s/%s::\n", $1, $2, a[2], a[1] }' \
        /etc/mail-users/users > /etc/dovecot/users
    chown root:dovecot /etc/dovecot/users
    chmod 0640 /etc/dovecot/users
    mkdir -p /var/mail
    chown vmail:vmail /var/mail
    exec dovecot -F
    ;;
transport)
    postconf -e 'maillog_file = /dev/stdout' 'inet_interfaces = all' 'inet_protocols = ipv4' \
        'mydestination =' 'smtputf8_enable = no' 'compatibility_level = 3.6' \
        'smtpd_tls_security_level = none' 'smtp_tls_security_level = none'
    # Containers have no use for Postfix's chroot.
    postconf -F '*/*/chroot = n'
    if [ -f /etc/postfix-site/site.cf ]; then
        grep -v '^[[:space:]]*\(#\|$\)' /etc/postfix-site/site.cf | while IFS= read -r line; do
            postconf -e "$line"
        done
    fi
    # Deferred mail lives on the volume and must outlive the pod.
    postfix set-permissions >/dev/null 2>&1 || true
    postfix check
    exec postfix start-fg
    ;;
*)
    echo "usage: mail-entrypoint mailbox|transport" >&2
    exit 64
    ;;
esac
