## Summary

Describe the problem and the focused change.

## Validation

- [ ] `./scripts/check` passes for code changes; documentation changes pass relevant link/example and whitespace checks.
- [ ] Lifecycle, parsing, or notification protocol changes include a focused regression test.
- [ ] Tests mock privileged and remote operations instead of changing real devices or hosts.
- [ ] User-visible behavior or requirements are documented.

## Security and Cleanup

Describe any effect on SSH commands, USB/IP exposure, `sudo` boundaries, device
selection, process ownership, or failure cleanup. Write `None` when not
applicable.

For notification changes, include service-name ownership, negotiated
capabilities, untrusted payloads, and action/close callback scope where relevant.
Keep fixture validation separate from real desktop/hardware acceptance.
