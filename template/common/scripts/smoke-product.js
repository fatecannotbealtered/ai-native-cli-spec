#!/usr/bin/env node
"use strict";

// Replace with a product-owned check against an authorized disposable target.
// `prepare <binary>` prints {"argv":[binary,"actual-command",...],
// "requirements":{"minimum_os":"...","cpu":"...","external_software":[],
// "system_libraries":[],"node_wrapper":"...","host":"..."}}. The checker
// executes that binary and argv itself, then pipes its stdout to
// `validate <binary>`. Validate the actual output and print {"observed":"..."};
// exit nonzero on failure. Never put secrets in argv or returned observations.
// State "none" when appropriate; for a remote host state its supported OS,
// CPU and protocol. State Node requirements separately from the direct binary.
// Redact secrets from observations and stderr; never invent a pass.
console.error("Implement scripts/smoke-product.js before publishing a supported package.");
process.exit(1);
