default: test

include ../../../helpers/examples.mk

RELEASE := logstash
TIMEOUT := 1200s

install:
	helm upgrade -n observe --wait --timeout=$(TIMEOUT) --install --values values.yaml $(RELEASE) ../../

test: install goss

purge:
	helm del $(RELEASE)
	kubectl delete $$(kubectl get pvc -l release=$(RELEASE) -o name)
