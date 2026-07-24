# Security Policy

## Supported Versions

`remote-chrome` does not currently publish versioned releases. Security fixes
are made on the latest `main` branch.

## Reporting a Vulnerability

Please do not open a public issue for a suspected vulnerability. Use
[GitHub's private vulnerability reporting](https://github.com/byebyebryan/remote-chrome/security/advisories/new)
instead.

Include the affected commit, local and remote Linux distributions, the command
or configuration involved, expected and actual behavior, security impact, and
a minimal reproduction when possible. Remove hostnames, usernames, SSH
material, hardware-token secrets, and other sensitive data from reports.

Security-sensitive areas include:

- SSH command and tunnel construction
- USB/IP network exposure
- YubiKey device selection, binding, attachment, and cleanup
- local and remote `sudo` boundaries
- lifecycle state and process ownership

Reports will be acknowledged as soon as practical. Validation and remediation
timelines depend on severity and reproducibility.
