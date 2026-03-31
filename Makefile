.PHONY: build compile test precommit

build:
	npx tsc --noEmit

compile:
	npx tsc

test: compile
	python3 tests/evolve_test.py
	$(MAKE) -C examples/hello test

precommit: build test
