# qgis-agent-plugin

QGIS Agent 插件本体。构建/部署/测试入口见 `package.json`。

## Git branches

Git flow. `main` is the releasable line and moves only by merging `develop` into it; `develop` is the integration line and takes no commits of its own, only merges of a branch cut from it.

| Branch | Cut from | Merged into |
| --- | --- | --- |
| `feature/<name>`, `fix/<name>`, `docs/<name>` | `develop` | `develop` |
| `release/<version>` | `develop` | `main`, then back into `develop` |
| `hotfix/<name>` | `main` | `main`, then into `develop` |

A change lands like this:

1. Cut a branch from `develop`: `git switch -c feature/<name> develop`.
2. Commit there, and merge with `--no-ff` so the branch stays visible in `git log`.
3. Merge it into `develop` and delete the branch.
4. For a release, merge `develop` into `main` (`--no-ff`) and push `main`.

Done means the change is on `develop` through a merge commit (`git log --merges -1 develop`) and the branch that carried it is deleted.

## Agent skills

### Issue tracker

GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Five state roles + two categories, label strings equal to role names. See `docs/agents/triage-labels.md`.

### Domain docs

single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
