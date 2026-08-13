# Releasing

Use this runbook to cut a release. Complete the steps in order.

1. Choose the new version under [the versioning rules](../AGENTS.md#versioning).
2. Set the version in `pyproject.toml`, `.claude-plugin/plugin.json`, and `.codex-plugin/plugin.json`.
3. Set the matching `@v<version>` tag in `.mcp.json`.
4. Prepare `CHANGELOG.md` as required by [AGENTS.md → Versioning](../AGENTS.md#versioning).
5. Run [the full gate](../AGENTS.md#tooling).
6. Have the maintainer merge the release pull request into `main`.
7. Create the matching `v<version>` tag from that commit.
8. Push the tag to the remote.
9. Verify that the tag exists on the remote.

The packaging tests compare both plugin manifests and the `.mcp.json` pin with the
`pyproject.toml` version. They do not check `CHANGELOG.md`. Nothing inside the repository can verify
the pushed tag. An unpushed tag makes `.mcp.json` unresolvable for every user, but leaves the
working tree green — so push the tag as part of the release, not after it.
