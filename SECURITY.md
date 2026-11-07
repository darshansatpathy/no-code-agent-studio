# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/darshansatpathy/no-code-agent-studio/security/advisories/new)
for this repository. You should get an acknowledgement within three working days.

## Scope

This project is a reference implementation. The [threat model](docs/threat-model.md)
lists what it is designed to defend against and what is deliberately out of scope.

## Operating it safely

- Put the studio behind a gateway that authenticates users and sets their roles, and set
  `STUDIO_SERVICE_TOKEN` so nothing else can assert a role.
- Keep the release-manager and approver groups small; review the audit log, especially
  promotions with acknowledged risks.
- Use the prod overlay (PostgreSQL, two or more replicas) for anything shared.
- Review every new tool added to the catalog: the platform, not the specs, decides what tools do.
