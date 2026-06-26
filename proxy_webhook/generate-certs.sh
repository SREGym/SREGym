#!/bin/bash
# Generates self-signed TLS certificates for the Mutating Webhook

set -e

NAMESPACE="webhook-system"
SERVICE="proxy-webhook-service"
SECRET="proxy-webhook-certs"

echo "Generating TLS certificates for ${SERVICE}.${NAMESPACE}.svc..."

# 1. Generate the private key
openssl genrsa -out tls.key 2048

# 2. Create a config file for the certificate
cat <<EOF > csr.conf
[req]
req_extensions = v3_req
distinguished_name = req_distinguished_name
[req_distinguished_name]
[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${SERVICE}
DNS.2 = ${SERVICE}.${NAMESPACE}
DNS.3 = ${SERVICE}.${NAMESPACE}.svc
EOF

# 3. Generate the certificate signing request and the self-signed cert
openssl req -new -key tls.key -subj "/CN=${SERVICE}.${NAMESPACE}.svc" -config csr.conf -out tls.csr
openssl x509 -req -in tls.csr -signkey tls.key -CAcreateserial -out tls.crt -days 365 -extensions v3_req -extfile csr.conf

# 4. Inject the CA Bundle into the mutating webhook config
CA_BUNDLE=$(cat tls.crt | base64 | tr -d '\n')
sed -i.bak "s/caBundle: .*/caBundle: ${CA_BUNDLE}/g" mutating-webhook-configuration.yaml
rm mutating-webhook-configuration.yaml.bak

echo "Certificates generated successfully!"
echo "CA Bundle injected into mutating-webhook-configuration.yaml"
