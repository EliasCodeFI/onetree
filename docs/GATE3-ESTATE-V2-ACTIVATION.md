# Gate3 dogfood activation on estate-v2

**Recorded 2026-11-10T14:53Z by the coordinator session on the Windows desk
(Claude Code).** This record describes the shared clone and its configuration
from the run's `SETUP-RECORD.md`. Details not in that setup record are unknown.

| Item | Value |
| --- | --- |
| Shared clone | `/home/v2/projects/onetree-loop1` in WSL2 distro `estate-v2` (user `v2`); listed in `/etc/v2/shared-clones`: 1 line; remotes: 0 |
| Base | `0adeed777bc67580ad3b560cad5c966564118b4d`, adopted from `/home/v2/v2/in/onetree-0adeed7.bundle` (SHA-256 `f4cf9c7befeb1a6efa17863d4c49a542dba78a8b1d9d6a1646bf0f2e19550766`) |
| Main at start of run | `769bb1914e483ba1e375ab4b823d15b27321cc3f`: the base plus one commit adding files at protected paths (`CLAUDE.md`, `.claude/settings.json`, `.mcp.json`, `.codex/README`) |
| Made by | `install/v2-clone adopt` from the estate-v2 build repository, run by the round-8 integration agent on 2026-11-10 at 10:16 UTC |
| Coordinator | `/home/v2/v2/bin/onetree` -> `/home/v2/v2/opt/onetree-0adeed7/.venv-cli/bin/onetree`; source `/home/v2/v2/opt/onetree-0adeed7` (onetree at `0adeed7`). Sessions coordinate with this build, never with the shared tree's source. |
| Dev environment | `/home/v2/v2/opt/onetree-loop1-dev` (made with uv; Python 3.12.3; no copy of onetree); Git 2.56.0 |
| Payload gate | `/usr/local/lib/v2/gates/v2_gate.py`, admitted at SHA-256 `ae5874935ed2b3240a741c24d01515d9aadfc771bd7670ef8312d1135a2f850f`; the setup record reports the same SHA-256 for the file at setup |
| Referee timeout | 1800 s, an operational choice, not a measured bound |
| Protection baseline | `refs/heads/main` protected by onetree (actor `owner`) at adoption, tip `769bb1914e48`; 35 onetree log events: 25 `adopted`, 9 `session_register`, 1 `workspace_protect`; divergences: 0 |

## Referee command

```sh
/usr/bin/python3 -I -S /usr/local/lib/v2/lib/v2/knownbad_sandbox.py referee --python /home/v2/v2/opt/onetree-loop1-dev/bin/python
```

## Baseline on main

The round-8 integration measured the referee command three times on 2026-11-10,
each alone under a 1,408 MiB memory cap:

| Run | Started (UTC) | Result | Wall time | Peak memory |
| --- | --- | --- | --- | --- |
| 1 | 10:21:18 | 1639 passed, 52 skipped, coverage 91.10% | 354.9 s | 936 MiB |
| 2 | 10:27:15 | 1639 passed, 52 skipped, coverage 91.10% | 373.1 s | 896 MiB |
| 3 | 10:33:30 | 1639 passed, 52 skipped, coverage 91.10% | 371.1 s | 954 MiB |

The setup record attributes these measurements to
`C:/Users/Spartallan/estate-v2/work/integrate/INTEGRATION-R8.md`, section 2.
Measurements of exact-path claims and session waiting times are unknown from the
setup record.
