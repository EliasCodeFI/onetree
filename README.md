<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/onetree-logo-dark.svg">
    <img src="docs/assets/onetree-logo-light.svg" alt="onetree" width="340">
  </picture>
</h1>

<p align="center"><strong>Your agents. One tree. No worktrees.</strong></p>

<p align="center">
  Several coding agents (Claude Code sessions, their subagents, Codex) working at the same time
  in one git clone and one working tree:<br>
  a private git index for each agent, claims on the files it writes, a broker that checks every
  commit, and your test suite as the referee.
</p>

<p align="center">
  <a href="https://github.com/EliasCodeFI/onetree/actions/workflows/ci.yml"><img src="https://github.com/EliasCodeFI/onetree/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI status"></a>
  <img src="https://img.shields.io/badge/release-0.0.1a0%20(pre--release)-orange" alt="Release 0.0.1a0, pre-release">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="Python 3.11, 3.12 and 3.13">
  <img src="https://img.shields.io/badge/runtime%20dependencies-0-brightgreen" alt="No runtime dependencies">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Licence: Apache 2.0"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#works-with-claude-code-and-codex">Claude Code and Codex</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#commands">Commands</a> ·
  <a href="docs/DESIGN.md">Design</a>
</p>

> [!NOTE]
> **Pre-release.** Commands and options may still change. If something does not work as you
> expect, [open an issue](https://github.com/EliasCodeFI/onetree/issues).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/how-onetree-works-dark.svg">
    <img src="docs/assets/how-onetree-works-light.svg" width="100%" alt="Animated diagram of onetree. Three coding agents share one git working tree. Each has a private index, claims the files it will write, and lands through a commit broker that audits the claims, runs a payload gate and the test suite, and then moves the branch.">
  </picture>
</p>

1. **Sessions: a private index for every agent.** Each agent registers a session and gets its own git index. Staging is private; the working tree is shared.
2. **Claims: exclusive write leases on paths.** Before editing, a session claims the exact paths it will write. A claim is an exclusive lease on a path.
3. **A refusal names the holder.** A claim on a held path is refused, and the refusal names the holder. A change to a path you did not claim cannot land.
4. **Edit in the shared tree, stage privately.** Agents edit their claimed files in the one working tree and stage them with ordinary git, each into its own index.
5. **Landing: every change goes through the broker.** onetree land audits the claims, runs the admitted payload gate and your tests, then moves the branch ref by compare-and-swap.
6. **A red candidate stays out.** If the tests fail on the candidate, the landing is refused. The branch does not move, and the refusal is recorded.
7. **One tree, one history.** One clone, one working tree, one linear history. Every claim, landing and refusal is an event in an append-only log kept in git: refs/onetree/wal.

<details>
<summary>Text description of the animation, step by step</summary>

1. **Sessions: a private index for every agent.** Each agent registers a session and gets its own git index. Staging is private; the working tree is shared. *In the picture:* Three agent cards (Agent A, Agent B, Agent C) stand on the left. In the middle is a panel titled "One shared working tree" with five files. On the right are the commit broker and the branch main. A box labelled "index" appears inside each agent card: each session has registered and received its own private git index. Three session_register events appear in the event log at the bottom.
2. **Claims: exclusive write leases on paths.** Before editing, a session claims the exact paths it will write. A claim is an exclusive lease on a path. *In the picture:* Agent A claims src/api.py, Agent B claims src/auth.py and Agent C claims tests/test_api.py. An arrow runs from each agent to its file, and the file row shows "claimed by A" (or B, or C) with a badge carrying the agent's letter. Three claim events are appended to the log.
3. **A refusal names the holder.** A claim on a held path is refused, and the refusal names the holder. A change to a path you did not claim cannot land. *In the picture:* Agent C also asks for src/api.py, which Agent A holds. The request is refused: the row shows "C refused: held by A" and a crossed-out badge with the letter C. A refused event is appended to the log.
4. **Edit in the shared tree, stage privately.** Agents edit their claimed files in the one working tree and stage them with ordinary git, each into its own index. *In the picture:* Each agent edits its own file in the shared working tree; the rows change to "edited by A", "edited by B" and "edited by C". Each agent then stages its file into its own index: the index box in each agent card now names the staged file.
5. **Landing: every change goes through the broker.** onetree land audits the claims, runs the admitted payload gate and your tests, then moves the branch ref by compare-and-swap. *In the picture:* Agent A lands. A chip labelled "candidate A" travels from A's index to the commit broker. The broker's four steps turn green one after another: claims audit, payload gate, test referee, compare-and-swap. A new commit marked A appears on the branch main, the file row shows "landed by A", and the events land_intent and landed are appended to the log.
6. **A red candidate stays out.** If the tests fail on the candidate, the landing is refused. The branch does not move, and the refusal is recorded. *In the picture:* Agent B tries to land. Its candidate passes the claims audit and the payload gate, but the test referee turns red: the tests fail on the candidate. The candidate is marked refused, a crossed-out circle shows that no commit was added to main, and a refused event is appended to the log.
7. **One tree, one history.** One clone, one working tree, one linear history. Every claim, landing and refusal is an event in an append-only log kept in git: refs/onetree/wal. *In the picture:* Agent C lands the same way as A: four green steps, and a second new commit, marked C, on main. The final picture: three agents, one working tree, a linear branch carrying the commits of A and C, B's change still waiting in the tree, and an event log with twelve entries.

</details>

## Works with Claude Code and Codex

Several Claude Code sessions or subagents on one repository, overwriting each other's files?
onetree keeps them in one working tree, without a git worktree for each: every file an agent is
about to edit is claimed for it first, a second agent asking for the same file is told who has
it, and finished work reaches the branch only after your tests pass.

For Claude Code: [install onetree](#install), set your repository up once, and add the plugin.

```sh
onetree init --test "python -m pytest -q"     # in your repository, once
```

```text
/plugin marketplace add EliasCodeFI/onetree
/plugin install onetree@onetree
```

The plugin claims each file before Claude edits it, refuses `git checkout`, `reset`, `stash`,
`commit` and the other git commands that would erase another agent's work, gives each subagent
claims of its own, and adds `claim`, `release`, `claims` and `land` tools. Claude lands finished
work with the `land` tool, which also works when Claude Code's shell sandbox is on.

For Codex: add the same hook to `.codex/hooks.json` in your repository, trust it with `/hooks`,
and land with `onetree land -a -m "<message>"`.

```json
{"hooks": {"PreToolUse": [{"matcher": "^(apply_patch|Bash)$",
  "hooks": [{"type": "command", "command": "onetree hook codex pre-tool-use"}]}]}}
```

### How it compares

| Approach | One working tree | Two agents, one file | Tests before the branch moves | Agents |
| --- | --- | --- | --- | --- |
| A git worktree per agent | No, one checkout each | Found later, as a merge conflict | Up to you | Any |
| Lock hooks ([claude-file-lock](https://github.com/DevsProtein/claude-file-lock), [claude-bulkhead](https://github.com/dooly6276/claude-bulkhead)) | Yes | Second edit refused | No | Claude Code |
| Claude Code agent teams | Yes | Not prevented: tasks are claimed, files are not | No | Claude Code |
| **onetree** | Yes | Second edit refused, with the holder's name | Yes, your test command | Claude Code, Codex, any command line |

## Why one tree

The usual way to run several coding agents on one repository is to give each agent its own git
worktree or clone and to merge the results afterwards. onetree is the other design: all agents work
in the same checkout, and the coordination a merge would do afterwards happens earlier.

- **Before an edit**, a claim decides which agent writes a file.
- **At the commit**, a broker decides what reaches the branch, and your tests run on the change
  before the branch moves.
- **Afterwards**, a log kept in git itself records every claim, landing and refusal.

onetree is a command-line tool with no server and no runtime dependencies. Its state lives in the
repository's own `.git` directory, so your working tree stays free of extra files.

## How it works

| Part | What it does | Tests |
| --- | --- | --- |
| **Private index per agent** | Every agent session stages into its own git index, so staging never collides. The working tree is shared; the staging area is not. | [`test_index_integrity.py`](tests/test_index_integrity.py) |
| **Claims** | `onetree claim <path>` gives a session the exclusive right to write a file. Asking for a file someone else holds is refused, and the refusal names the holder. | [`test_claims.py`](tests/test_claims.py) |
| **Commit broker** | `onetree land` is the only way into the shared branch. It checks that every changed file is claimed by the session, builds the commit from the session's own index, runs the payload gate you admitted, and moves the branch only if nobody moved it in the meantime. If someone did, it rebuilds the commit on top and tries again. | [`test_land.py`](tests/test_land.py) · [`test_payload_gate.py`](tests/test_payload_gate.py) |
| **Test referee** | Before the branch moves, your test command runs on a temporary checkout of the change. If the tests fail or time out, the change is refused. | [`test_referee.py`](tests/test_referee.py) |

Everything onetree records goes into an append-only log on the git ref `refs/onetree/wal` (a
write-ahead log), and every branch and log update is a compare-and-swap. The details are in
[`docs/DESIGN.md`](docs/DESIGN.md).

### The landing path

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/onetree-landing-path-dark.svg">
    <img src="docs/assets/onetree-landing-path-light.svg" width="100%" alt="Animated diagram of a onetree landing. A candidate commit built from a session&#x27;s private index passes four steps in order (claims audit, payload gate, test referee, compare-and-swap) before it becomes a commit on the branch main. One candidate with an unclaimed path is refused at the audit, and of two racing candidates one wins and the other is rebuilt and tried again.">
  </picture>
</p>

1. **A candidate is built from the private index.** onetree land builds a candidate commit from the session's private index. The shared index and the working tree are not touched.
2. **Four steps before the branch moves.** Each step can refuse. Only a candidate that passes all four moves the ref. A landing is recorded in the append-only log as an intent and a landed event.
3. **One unclaimed path refuses it all.** If one changed path was not claimed by this session, the whole candidate is refused. Nothing reaches the branch.
4. **A race has exactly one winner.** When two landings race, the compare-and-swap lets exactly one move the ref. The other is rebuilt on the new head and goes through the steps again.
5. **What reaches the branch.** Every commit on the branch is the exact candidate that was audited, gated and tested. A refused candidate leaves the branch where it was.

<details>
<summary>Text description of the animation, step by step</summary>

1. **A candidate is built from the private index.** onetree land builds a candidate commit from the session's private index. The shared index and the working tree are not touched. *In the picture:* A row of boxes runs from left to right: a session with its private index, four numbered steps (claims audit, payload gate, test referee, compare-and-swap) and the branch main. A rail connects them. A token marked A, the candidate commit of Agent A, appears at the session's index.
2. **Four steps before the branch moves.** Each step can refuse. Only a candidate that passes all four moves the ref. A landing is recorded in the append-only log as an intent and a landed event. *In the picture:* The token A moves along the rail and stops at each step. Each step turns green as the candidate passes it: the claims audit, the payload gate, the test referee (shown working for a moment before it turns green) and the compare-and-swap. The token then moves onto the branch main and becomes a new commit marked A.
3. **One unclaimed path refuses it all.** If one changed path was not claimed by this session, the whole candidate is refused. Nothing reaches the branch. *In the picture:* A token marked B starts from the index. The index now lists a second file, src/db.py, marked "not claimed". The token stops at the first step, the claims audit, which turns red and shows the reason unclaimed_paths. The token is crossed out and disappears. The branch main is unchanged.
4. **A race has exactly one winner.** When two landings race, the compare-and-swap lets exactly one move the ref. The other is rebuilt on the new head and goes through the steps again. *In the picture:* Two tokens, C and B, travel through the first three steps one behind the other. At the compare-and-swap, C arrives first and becomes a new commit on main. For B the step turns amber: the head has moved. B travels back along a dashed return line labelled "moved head: rebuilt on the new head, tried again", passes all four steps again and becomes the next commit on main, after C.
5. **What reaches the branch.** Every commit on the branch is the exact candidate that was audited, gated and tested. A refused candidate leaves the branch where it was. *In the picture:* The final picture: all four steps are green and the branch main carries three new commits in the order A, C, B, after the commit it started with.

</details>

## Install

onetree is pre-release and **is not on PyPI**, so install it from this repository. `pip install
onetree` does not install this project.

**You need:** Linux, Python 3.11 or newer, and Git. There are no other dependencies.

With [uv](https://docs.astral.sh/uv/), as an isolated command on your `PATH`:

```sh
uv tool install git+https://github.com/EliasCodeFI/onetree
```

With [pipx](https://pipx.pypa.io/):

```sh
pipx install git+https://github.com/EliasCodeFI/onetree
```

With pip, inside a virtual environment:

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install git+https://github.com/EliasCodeFI/onetree
```

Check the installation:

```console
$ onetree status
onetree 0.0.1a0
```

To pin an exact version, add a commit to the address, for example
`git+https://github.com/EliasCodeFI/onetree@<commit>`. To work on onetree itself, see
[`CONTRIBUTING.md`](.github/CONTRIBUTING.md).

## Quick start

**1. Once per repository.**

```sh
cd /path/to/your/repository
onetree init --test "python -m pytest -q"
```

`init` admits onetree's standard payload gate, sets the test command that runs before every
landing (in a temporary checkout, without a shell, 300 seconds at most by default) and turns on
the log cache. The standard gate refuses unresolved merge conflict markers, private keys and files
over 50 MiB. `--gate <file>` admits a gate of your own instead, and `--protect` also refuses
landings after anyone moves the branch outside onetree.

**2. In every agent session.** The Claude Code plugin does all of this for you. By hand:

```sh
export ONETREE_SESSION_ID=agent-a      # Claude Code and Codex sessions are recognised without it

onetree session                        # registers the session, once
onetree claim "$PWD/src/api.py"        # absolute paths; the file may not exist yet

# ... edit src/api.py in the shared working tree ...

onetree land -a -m "api: handle empty input"
onetree release "$PWD/src/api.py"
```

`land -a` stages exactly the files the session holds, so other agents' unfinished edits in the
shared tree stay out of the commit. It first moves the session to the branch's tip, keeping its
claims, so work lands cleanly after other agents have landed.

A session lives as long as the process it was registered for, and for at most 24 hours by default
(`--ttl`). That process is the parent of `onetree session`, normally your shell; in Claude Code it
is Claude Code itself. A harness that starts a new shell for every command names its own
long-lived process instead, with `onetree session --pid <pid>`.

A session that asks for a file another session holds gets an error that names the holder, for
example `onetree: claim refused; held by agent-a`. With `--json` the error has the type
`claim_conflict` and lists the paths and the holders.

**The payload gate.** A repository with no admitted gate refuses every landing. The broker runs the
gate on each change as `python -I -S commit_payload.py commit <commit> --repo <repository> --json`
and lands the change only when the gate answers `CLEAN`. `onetree gate --admit <file>` admits
another gate at any time.

**A demo without your own repository.** `python -m onetree.demo --output /tmp/onetree-demo` runs
several worker processes against a new sample repository it creates in that folder: a claim refusal
first, then landings through the broker and the standard gate. The result is in `report.json` there.

## Commands

Every command accepts `--json` and then writes exactly one JSON object to standard output.

| Command | What it does |
| --- | --- |
| `onetree init [--test <command>]` | Set a repository up: payload gate, test command, log cache. |
| `onetree status` | Report the version. |
| `onetree session` | Register or inspect a session index. |
| `onetree claim <path>...` | Claim one or more absolute paths; a file may not exist yet. |
| `onetree claims` | Show who holds which paths. |
| `onetree check <path>...` | Check one or more absolute paths; exit status 1 when another live session holds one. |
| `onetree narrow <parent> <path>...` | Atomically retain descendants of a held path. |
| `onetree release [<path>...]` | Release selected paths or all held paths. |
| `onetree land -a -m <message>` | Stage every file the session holds, then audit and land it on the branch. Without `-a`, land what is staged. |
| `onetree gate [--admit <file>]` | Inspect or admit the repository's commit payload gate. |
| `onetree protect` | Protect the current shared branch baseline. |
| `onetree reconcile` | Explicitly accept an inspected workspace baseline. |
| `onetree wal [--recover]` | Verify and inspect `refs/onetree/wal`; resolve any exposed intent. |
| `onetree autosave save\|list\|restore\|status` | Save, list or restore snapshots of claimed working bytes. |
| `onetree gc` | Append a stale-session and stale-claim collection event. |
| `onetree hook claude\|codex <event>` | Run as a Claude Code or Codex hook (the plugin uses it). |
| `onetree mcp` | Serve `claim`, `release`, `claims` and `land` to an agent over MCP. |

## Good to know

- Claims cover files in the repository. They do not keep agents apart at run time: two agents
  that use the same port or database still meet there.
- onetree works on one machine and one clone. Pushing to GitHub or another remote is up to you.
- Claims guard what goes through the agent's edit tools and through onetree's landing. A shell
  command that writes a file directly, such as `sed -i`, is not stopped; a landing still refuses
  any file the session does not hold.
- Linux only.

## Documentation

- [`docs/DESIGN.md`](docs/DESIGN.md): how it works, in detail.
- [`docs/llms.txt`](docs/llms.txt): a short summary for language models.

## Contributing and security

Issues and pull requests are welcome: see [`CONTRIBUTING.md`](.github/CONTRIBUTING.md). Report a
vulnerability privately, as [`SECURITY.md`](.github/SECURITY.md) describes, not in an issue.

## Credits

onetree's design learned from [safegit](https://github.com/stricttools/safegit),
[Agentlocks](https://github.com/simke9445/agentlocks),
[Flightdeck](https://github.com/justinchuby/flightdeck),
[mergetrain](https://github.com/yongjip/mergetrain), and [grite](https://github.com/neul-labs/grite)
and [brat](https://github.com/neul-labs/brat) by neul-labs.

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
