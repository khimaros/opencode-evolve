
OPENCODE_BASE := $(PWD)/../../anomalyco/opencode/
OPENCODE_BIN := $(OPENCODE_BASE)/packages/opencode/dist/opencode-linux-x64/bin/opencode

build:
	npx tsc --noEmit
.PHONY: build

compile:
	npx tsc
.PHONY: compile

test: compile
	python3 tests/evolve_test.py
	$(MAKE) -C examples/hello test
.PHONY: test

test-integration:
	OPENCODE_BIN=$(OPENCODE_BIN) python3 ./tests/opencode_integration_test.py
.PHONY: test-integration

precommit: build test test-integration
.PHONY: precommit
