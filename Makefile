.PHONY: build test precommit

build:
	npx tsc --noEmit

test:
	$(MAKE) -C examples/hello test

precommit: build test
