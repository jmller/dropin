# Dropin. No build step: stdlib-only Python, run from the repo
# root. Integration targets need the pinned binaries; without them the suite
# reports skipped, never passes.

PYTHON ?= python3
RESTIC ?= $(DROPIN_RESTIC_BIN)
RCLONE ?= $(DROPIN_RCLONE_BIN)

.PHONY: test test-integration test-all probe install-user clean

## Fakes only, no external binaries: discovery, unit, pipeline, contract.
test:
	$(PYTHON) -m unittest discover -s tests -t . -p 'test_discovery.py' -v
	$(PYTHON) -m unittest discover -s tests/unit -t . -v
	$(PYTHON) -m unittest discover -s tests/pipeline -t . -v
	$(PYTHON) -m unittest discover -s tests/contract -t . -v

## The real pinned restic over an rclone local remote; skipped without binaries.
test-integration:
	$(PYTHON) -m unittest discover -s tests/integration -t . -v

## Everything, in one discovery pass.
test-all:
	$(PYTHON) -m unittest discover -s tests -t . -v

## Install a pip-free user command at ~/.local/bin/dropin (override with --target).
install-user:
	$(PYTHON) scripts/install-user.py

## Restic contract probe: records the restic behaviours the adapter relies on.
## Disposable data only, in a temporary directory.
probe:
	@test -n "$(RESTIC)" -a -n "$(RCLONE)" || \
		{ echo "set DROPIN_RESTIC_BIN and DROPIN_RCLONE_BIN"; exit 2; }
	RESTIC_BIN=$(RESTIC) RCLONE_BIN=$(RCLONE) $(PYTHON) scripts/restic_contract_probe.py

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
