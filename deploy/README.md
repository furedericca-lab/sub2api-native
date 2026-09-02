# Deployment files

- compose.yaml is the source-build entry point for the local
  sub2api-native:local image.
- docker-compose.yml runs that existing image without building or pulling.
- update.sh is the normal update entry point.
- check-mailbox-handoff.sh is the read-only pre-deploy gate.

The complete development, build, deployment, migration, upgrade, and rollback
contract is in ../AGENTS.md.
